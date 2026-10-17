"""Offline browser acceptance of diagnostics, audit counts and real queued computation."""
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
from quantify import datasets,factors
from quantify.jobs import QueueManager
from quantify.research import demo_csv
from quantify.workbench import Workbench
from playwright.sync_api import sync_playwright,expect

slow=False
original=QueueManager._spawn

def spawn(manager,job):
    if slow and job['kind']=='diagnostic':return subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    return original(manager,job)

with tempfile.TemporaryDirectory() as temp,patch.object(QueueManager,'_spawn',spawn):
    wb=Workbench(temp)
    snap=datasets.import_snapshot(wb.store,kind='monthly',raw_csv=demo_csv(),source='诊断合成',price_basis='total_return_index',is_demo=True)
    fs=wb.store.list('factor');momentum=next(f for f in fs if f['kind']=='momentum');quality=next(f for f in fs if f['kind']=='quality')
    constant=factors.create_version(wb.store,parent_id=momentum['id'],name='并列常数',reason='浏览器验证',expression='1')
    e=wb.run(snapshot_id=snap['id'],factor_ids=[momentum['id'],quality['id']]);wb.run(**e['request'])
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/diagnostics')
            expect(page.locator('#status')).to_have_text('操作完成。')
            expect(page.locator('#trial-counts')).to_contain_text('2 / 1')
            page.locator('#diagnostic-factors input[value="'+constant['id']+'"]').check()
            page.locator('#diagnostic-form button').click()
            expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
            expect(page.locator('#result-note')).to_contain_text('合成数据')
            expect(page.locator('#metrics')).to_contain_text('并列常数')
            expect(page.locator('#diagnostic-factors input[value="'+constant['id']+'"]')).to_be_checked()
            expect(page.locator('#metrics')).to_contain_text('未定义')
            first=json.loads(page.locator('#result-json').text_content())
            assert not first['report']['protocol']['holdout_evaluated']
            const=next(f for f in first['report']['factors'] if f['factor']['id']==constant['id'])
            assert const['summary']['ic_windows']==0 and const['summary']['score_coverage']==1
            assert len(first['report']['correlations'])==6
            with page.expect_download() as download:page.locator('#export').click()
            assert json.loads(Path(download.value.path()).read_text())==first
            page.locator('#replay').click()
            expect(page.locator('#job-info')).to_contain_text('读取固定报告',timeout=20000)
            expect(page.locator('#job-info')).to_contain_text('succeeded')
            expect(page.locator('#reports tbody tr')).to_have_count(1)
            page.select_option('#trial-scope','experiment')
            expect(page.locator('#status')).to_have_text('操作完成。')
            with page.expect_download() as download:page.locator('#export-trials').click()
            audit=json.loads(Path(download.value.path()).read_text());assert audit['scope']=='same_protocol' and audit['counts']['experiments']==2
            slow=True;page.locator('#replay').click()
            expect(page.locator('#cancel')).to_be_enabled()
            expect(page.locator('#status')).to_have_text('操作完成。');page.locator('#cancel').click()
            expect(page.locator('#job-info')).to_contain_text('cancelled',timeout=10000)
            expect(page.locator('#retry')).to_be_enabled();expect(page.locator('#status')).to_have_text('操作完成。')
            slow=False;page.locator('#retry').click()
            expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=20000)
            page.reload();expect(page.locator('#reports tbody tr')).to_have_count(1)
            page.get_by_role('button',name='查看',exact=True).click()
            expect(page.locator('#groups')).to_contain_text('并列常数')
            page.screenshot(path='/tmp/quantify-diagnostics-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'mobile overflow'
            assert not errors,errors
            browser.close()
            print('PASS: factor selection, real diagnostic worker, undefined constant IC/tie groups, paired correlations, stability, JSON exports, scoped trial counts, cached replay, cancellation/retry, reload and mobile; no JS errors')
    finally:
        server.shutdown();server.server_close();thread.join()
        if hasattr(server,'lab'):server.lab.close()
