"""Offline browser acceptance with real portfolio workers and explicit cancellation fixture."""
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
from playwright.sync_api import sync_playwright, expect

original=QueueManager._spawn
slow=False

def spawn(manager,job):
    if slow and job['kind']=='portfolio':
        return subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    return original(manager,job)

with tempfile.TemporaryDirectory() as temp,patch.object(QueueManager,'_spawn',spawn):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/portfolio')
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.locator('#demo').click()
            expect(page.locator('#specs')).to_contain_text('合成组合账本示例')
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.get_by_role('button',name='提交回放',exact=True).click()
            expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
            expect(page.locator('#metrics')).to_contain_text('等权全池')
            expect(page.locator('#result-note')).to_contain_text('合成数据')
            first=json.loads(page.locator('#result-json').text_content())
            assert first['run']['report']['is_demo']
            assert len(page.locator('#nav-chart polyline').all())==2
            assert first['run']['report']['protocol']['holdout_evaluated'] is False
            with page.expect_download() as download:page.locator('#export').click()
            assert json.loads(Path(download.value.path()).read_text())==first
            page.locator('#replay').click()
            expect(page.locator('#runs tbody tr')).to_have_count(2,timeout=20000)
            expect(page.locator('#job-info')).to_contain_text('succeeded')
            expect(page.locator('#result-json')).not_to_contain_text(first['run']['id'])
            second=json.loads(page.locator('#result-json').text_content())
            assert second['run']['report']==first['run']['report']
            page.fill('#name','父版本派生组合');page.select_option('#parent',first['spec']['id'])
            page.fill('#initial_cash','50000');page.locator('#spec-form button').click()
            expect(page.locator('#specs tbody tr')).to_have_count(2)
            expect(page.locator('#status')).to_have_text('操作完成。')
            slow=True
            page.locator('#specs tbody tr').filter(has_text='父版本派生组合').get_by_role('button',name='提交回放',exact=True).click()
            expect(page.locator('#cancel')).to_be_enabled()
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.locator('#cancel').click()
            expect(page.locator('#job-info')).to_contain_text('cancelled',timeout=10000)
            expect(page.locator('#retry')).to_be_enabled()
            expect(page.locator('#status')).to_have_text('操作完成。')
            slow=False;page.locator('#retry').click()
            expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
            expect(page.locator('#result-json')).to_contain_text('父版本派生组合')
            third=json.loads(page.locator('#result-json').text_content())
            assert third['spec']['parent_id']==first['spec']['id']
            assert third['run']['report']['selected']['summary']['initial_cash']==50000
            page.reload();expect(page.locator('#runs tbody tr')).to_have_count(3)
            page.locator('#runs tbody tr').first.get_by_role('button',name='查看',exact=True).click()
            expect(page.locator('#metrics')).to_contain_text('筛选组合')
            page.screenshot(path='/tmp/quantify-portfolio-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'mobile overflow'
            assert not errors,errors
            browser.close()
            print('PASS: synthetic preparation, immutable parent version, real worker, dual NAV, holdings, JSON export, replay, process cancel/retry, persisted runs, mobile; no JS errors')
    finally:
        server.shutdown();server.server_close();thread.join()
        if hasattr(server,'lab'):server.lab.close()
