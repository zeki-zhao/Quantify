import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Handler


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def request(self,path,payload=None):
        data=None if payload is None else json.dumps(payload).encode()
        return urlopen(Request(self.base+path,data=data,headers={"Content-Type":"application/json"}),timeout=20)

    def test_assets_and_symbols(self):
        for path in ('/','/app.js','/style.css','/api/research-template.csv'):
            with self.request(path) as response:
                self.assertEqual(response.status,200)
        with self.request('/api/stocks') as response:
            stocks=json.load(response)
            self.assertEqual(len(stocks),5)
            self.assertTrue(all(s['is_demo'] for s in stocks))

    def test_analyze_and_research_exports(self):
        payload={"symbol":"601318.SH","basis":"pev","scenarios":[{"amount":15000,"multiple":.7,"note":"演示"}]*3,"simulation":{"paths":100}}
        with self.request('/api/analyze',payload) as response:
            body=json.load(response)
            self.assertEqual(body['report']['simulation']['calibration'],'未经校准')
            self.assertIn('stock.price.adjustment',body['csv'])
        with self.request('/api/research',{}) as response:
            self.assertGreater(len(json.load(response)['report']['folds']),0)

    def test_invalid_inputs_and_paths(self):
        for path,payload,status in [('/api/analyze',{},400),('/api/analyze',[],400),('/api/analyze',{'symbol':'bad'},400),('/../app.py',None,404),('/api/research',{'horizon':4},400)]:
            with self.subTest(path=path),self.assertRaises(HTTPError) as error:
                self.request(path,payload)
            self.assertEqual(error.exception.code,status)

    def test_slow_response_does_not_keep_computation_busy(self):
        response_started, release_response = threading.Event(), threading.Event()
        first_response = threading.Lock()
        class SlowHandler(Handler):
            def send_json(self, status, value):
                if first_response.acquire(blocking=False):
                    response_started.set()
                    release_response.wait(5)
                super().send_json(status, value)
        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        statuses = []
        def request():
            try:
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}/api/analyze',
                             data=b'{}', headers={'Content-Type': 'application/json'}), timeout=10) as response:
                    statuses.append(response.status)
            except HTTPError as error:
                statuses.append(error.code)
                error.close()
        client = threading.Thread(target=request, daemon=True)
        try:
            client.start()
            self.assertTrue(response_started.wait(3))
            request()
            self.assertEqual(statuses, [400])
        finally:
            release_response.set(); client.join(timeout=5)
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(statuses, [400, 400])


if __name__ == '__main__':
    unittest.main()
