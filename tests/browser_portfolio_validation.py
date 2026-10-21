"""Offline browser acceptance of frozen portfolio validation and manual version changes."""
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
from test_portfolio_validation import build
from playwright.sync_api import sync_playwright,expect

slow=False
original=QueueManager._spawn

def spawn(manager,job):
    if slow and job['kind']=='portfolio_validation':
        code='''import sys,time
from quantify.workbench import Workbench
from quantify.jobs import JobStore
from quantify.exposures import reserve
s=JobStore(sys.argv[1]);j=s.job(sys.argv[2]);f=s.get('portfolio_freeze',j['payload']['freeze_id'])
reserve(s,'portfolio:'+f['id'],f['start'],f['end'],f['symbols'])
s.checkpoint(j['id'],sys.argv[3],dict(stage='reserved-test'))
time.sleep(30)
'''
        return subprocess.Popen([sys.executable,'-c',code,str(manager.store.directory),job['id'],manager.owner])
    return original(manager,job)

with tempfile.TemporaryDirectory() as temp,patch.object(QueueManager,'_spawn',spawn):
    w=Workbench(temp);one=build(w);two=build(w,symbols_prefix='R')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url+'/portfolio-validation');expect(page.locator('#status')).to_have_text('操作完成。')
            def freeze(bundle,name):
                page.locator('#development-run').select_option(bundle['run']['id']);page.locator('#freeze-name').fill(name)
                page.locator('#freeze-form button').click();expect(page.locator('#freeze-note')).to_contain_text(name)
                expect(page.locator('#status')).to_have_text('操作完成。')
                return next(f for f in w.store.list('portfolio_freeze') if f['name']==name)
            def evaluate(f):
                page.locator('#freezes tr').filter(has_text=f['name']).get_by_role('button').click()
                expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
                expect(page.locator('#result-json')).to_contain_text(f['id'])
            def change(action,target=None,reason='浏览器合成流程'):
                page.locator('#change-action').select_option(action)
                if target:page.locator('#target-freeze').select_option(target)
                page.locator('#change-reason').fill(reason);page.locator('#change-form button').click()
                expect(page.locator('#status')).to_have_text('操作完成。')
            first=freeze(one,'首个合成组合');evaluate(first)
            expect(page.locator('#decision')).to_contain_text('通过组合独立验收')
            expect(page.locator('#metrics')).to_contain_text('100.000%')
            with page.expect_download() as download:page.locator('#export').click()
            report=json.loads(Path(download.value.path()).read_text());assert report['evaluation']['accepted']
            assert report['evaluation']['report']['selected']['curve'][0]['position_value']==0
            change('activate',first['id']);expect(page.locator('#active')).to_contain_text(first['id'])
            second=freeze(two,'第二个合成组合');evaluate(second);change('replace',second['id'])
            expect(page.locator('#active')).to_contain_text(second['id'])
            change('rollback',first['id']);expect(page.locator('#active')).to_contain_text(first['id'])
            change('retire');expect(page.locator('#active')).to_contain_text('当前组合研究版本：无')
            assert page.locator('#events tbody tr').count()==4
            duplicate=freeze(one,'重复留出只可探索');evaluate(duplicate)
            expect(page.locator('#result-note')).to_contain_text('已有相交')
            assert not w.store.get('portfolio_evaluation',duplicate['id'])['accepted']
            page.locator('#freezes tr').filter(has_text=first['name']).get_by_role('button').click()
            expect(page.locator('#job-info')).to_contain_text('读取完整缓存',timeout=20000)
            interrupted=freeze(one,'取消后保留暴露');slow=True
            page.locator('#freezes tr').filter(has_text=interrupted['name']).get_by_role('button').click()
            expect(page.locator('#job-info')).to_contain_text('reserved-test',timeout=20000)
            page.locator('#cancel').click();expect(page.locator('#job-info')).to_contain_text('cancelled',timeout=20000)
            slow=False;page.locator('#retry').click();expect(page.locator('#job-info')).to_contain_text('failed',timeout=20000)
            expect(page.locator('#job-info')).to_contain_text('已开始验收')
            page.reload();expect(page.locator('#status')).to_have_text('操作完成。');assert page.locator('#events tbody tr').count()==4
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path='/tmp/quantify-012-validation-mobile.png',full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'mobile overflow'
            assert not errors,errors;browser.close()
        print('PASS: freeze, actual validation worker, period metrics, export/cache, activation/replace/rollback/retire, duplicate rejection, cancellation after reservation and unsafe retry rejection, persistence/mobile')
    finally:
        if hasattr(server,'lab'):server.lab.close()
        server.shutdown();server.server_close();thread.join()
