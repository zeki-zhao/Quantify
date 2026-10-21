"""Offline acceptance: multi-factor creation, real queued increments and manual AI additions."""
import json
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify.jobs import QueueManager
from quantify.workbench import Workbench
from playwright.sync_api import sync_playwright,expect

slow=False
original=QueueManager._spawn

def spawn(manager,job):
    if slow and job['kind']=='increment':return subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    return original(manager,job)

with tempfile.TemporaryDirectory() as temp,patch.object(QueueManager,'_spawn',spawn):
    wb=Workbench(temp);snap=wb.post('/api/workspace/demo',{})
    fs={f['kind']:f for f in wb.store.list('factor')}
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url+'/increments');expect(page.locator('#status')).to_have_text('操作完成。')
            for k in ('momentum','quality'):page.locator('#components input[type="checkbox"][value="'+fs[k]['id']+'"]').check()
            page.locator('#blend-name').fill('浏览器双因子');page.locator('#blend-reason').fill('固定版本流程验证')
            page.locator('#blend-form button').click();expect(page.locator('#saved-blend')).to_contain_text('已保存')
            blend=next(f for f in wb.store.list('factor') if f['kind']=='blend')
            experiment=wb.run(snapshot_id=snap['id'],factor_ids=[blend['id']])
            page.locator('#refresh').click();expect(page.locator('#status')).to_have_text('操作完成。')
            page.locator('#experiment').select_option(experiment['id']);page.locator('#candidate-factor').select_option(fs['value']['id'])
            page.locator('#increment-form button').click();expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
            expect(page.locator('#result-note')).to_contain_text('合成数据');expect(page.locator('#metrics')).to_contain_text('加入候选后')
            expect(page.locator('#ablations')).to_contain_text('盈利质量')
            report=json.loads(page.locator('#result-json').text_content());assert report['report']['summary']['paired_windows']==8
            assert not report['report']['protocol']['holdout_evaluated']
            with page.expect_download() as download:page.locator('#export').click()
            assert json.loads(Path(download.value.path()).read_text())==report
            first_job=page.locator('#job-info').text_content().split()[1]
            page.locator('#replay').click();expect(page.locator('#job-info')).to_contain_text('读取固定报告',timeout=20000)
            assert first_job!=page.locator('#job-info').text_content().split()[1]
            page.locator('#reference-factor').select_option(fs['quality']['id']);page.locator('#references').click()
            expect(page.locator('#reference-json')).to_contain_text(experiment['id'])
            expect(page.locator('#reference-json')).to_contain_text(blend['id'])
            slow=True;page.locator('#increment-form button').click();expect(page.locator('#job-info')).to_contain_text('running',timeout=20000)
            page.locator('#cancel').click();expect(page.locator('#job-info')).to_contain_text('cancelled',timeout=20000)
            expect(page.locator('#retry')).to_be_enabled();slow=False;page.locator('#retry').click()
            expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path='/tmp/quantify-011-increments-mobile.png',full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
            page.goto(url+'/lab');expect(page.locator('#status')).to_have_text('操作完成。')
            page.locator('#baseline').select_option(experiment['id']);page.locator('#research-mode').select_option('augment')
            page.locator('#rounds').fill('1');page.locator('#candidates').fill('1');page.locator('#session-form button[type="submit"]').click()
            expect(page.locator('#manual')).to_be_visible();page.locator('#package').click()
            expect(page.locator('#context')).to_have_value(__import__('re').compile('"mode": "augment"'))
            page.locator('#proposals').fill(json.dumps(dict(action='propose',name='价值候选',expression='value',rationale='合成流程')))
            page.locator('#import').click();expect(page.locator('#job-info')).to_contain_text('已完成',timeout=20000)
            expect(page.locator('#job-json')).to_contain_text('augmented_minus_base')
            page.locator('#package').click();expect(page.locator('#context')).to_have_value(__import__('re').compile('\"increment\"'));package=json.loads(page.locator('#context').input_value())
            assert package['progress']['outcomes'][0]['increment']['status']=='completed'
            assert all(x not in json.dumps(package) for x in ('raw_csv','raw_import','DEMO1'))
            assert not errors,errors;browser.close()
        print('PASS: blend creation; paired/ablation report; download/replay; references; cancel/retry; mobile; manual AI augment')
    finally:
        if hasattr(server,'lab'):server.lab.close()
        server.shutdown();server.server_close();thread.join()
