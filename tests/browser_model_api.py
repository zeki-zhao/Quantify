"""Browser/API/real worker integration with explicitly synthetic upstream model responses."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify.jobs import QueueManager
from quantify.workbench import Workbench
from model_fixture_worker import fixture_spawn
from playwright.sync_api import sync_playwright, expect

with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'OPENAI_API_KEY':'browser-private-openai','DEEPSEEK_API_KEY':'browser-private-deepseek'}), patch.object(QueueManager,'_spawn',fixture_spawn):
    wb=Workbench(temp);snapshot=wb.post('/api/workspace/demo',{})
    factor=next(f for f in wb.store.list('factor') if f['kind']=='momentum')
    baseline=wb.run(snapshot_id=snapshot['id'],factor_ids=[factor['id']])
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/lab')
            expect(page.locator('#model-status')).to_contain_text('已配置（未联网验证）')
            for provider,currency in [('openai','USD'),('deepseek','CNY')]:
                page.select_option('#model-provider',provider)
                page.fill('#model-name','fixture-model');page.select_option('#model-currency',currency)
                page.fill('#model-input-price','0.1');page.fill('#model-output-price','0.4')
                page.locator('#model-form button').click()
                expect(page.locator('#model-status')).to_contain_text('已保存估算单价')
                expect(page.locator('#status')).to_have_text('操作完成。')
                page.select_option('#provider',provider)
                expect(page.locator('#api-options')).to_be_visible()
                expect(page.locator('#api-ready')).to_contain_text(currency)
                page.fill('#rounds','3');page.fill('#api-calls','3')
                page.locator('#session-form button').click()
                expect(page.locator('#job-info')).to_contain_text('已完成',timeout=15000)
                expect(page.locator('#model-usage')).to_contain_text('请求 2/3')
                expect(page.locator('#model-usage')).to_contain_text(currency)
                expect(page.locator('#outcomes')).to_contain_text('未改善')
                expect(page.locator('#outcomes')).to_contain_text('提案要求停止')
                expect(page.locator('#manual')).to_be_hidden()
                record=json.loads(page.locator('#job-json').text_content())
                assert record['payload']['provider']==provider and len(record['model_calls'])==2
                assert record['model_usage']['charged_tokens']==320
                assert not record['result']['activated']
                for call in record['model_calls']:
                    assert call['status']=='succeeded'
                    encoded=json.dumps(call['request'])
                    assert 'raw_csv' not in encoded and 'DEMO1' not in encoded
                with page.expect_download() as download:page.locator('#export-job').click()
                exported=Path(download.value.path()).read_text()
                assert provider in exported and 'browser-private-' not in exported
            page.reload();expect(page.locator('#jobs tbody tr')).to_have_count(2)
            page.select_option('#model-provider','deepseek')
            expect(page.locator('#model-name')).to_have_value('fixture-model')
            expect(page.locator('#model-output-price')).to_have_value('0.4')
            assert 'browser-private-' not in page.locator('body').text_content()
            page.screenshot(path='/tmp/quantify-model-api-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'mobile overflow'
            assert not errors,errors
            browser.close()
            print('PASS: both model configurations, frozen sessions, real child API proposal/experiment/feedback/stop, usage/cost display, JSON export without keys, reload and mobile; model responses are synthetic')
    finally:
        server.shutdown();server.server_close();thread.join()
        if hasattr(server,'lab'):server.lab.close()
