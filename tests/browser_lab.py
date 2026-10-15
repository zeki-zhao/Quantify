"""Optional real-browser test: python tests/browser_lab.py (preinstalled Playwright/Chromium)."""
import json
import re
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify.workbench import Workbench
from playwright.sync_api import sync_playwright, expect

with tempfile.TemporaryDirectory() as temp:
    wb = Workbench(temp)
    snapshot = wb.post('/api/workspace/demo', {})
    factor = next(f for f in wb.store.list('factor') if f['kind'] == 'momentum')
    baseline = wb.run(snapshot_id=snapshot['id'], factor_ids=[factor['id']])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.data_dir = temp
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
            page = browser.new_page(viewport={'width':1280,'height':900}, accept_downloads=True)
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            base = f'http://127.0.0.1:{server.server_port}'
            page.goto(base + '/lab')
            expect(page.locator('#baseline option')).to_have_count(1)
            page.locator('#rounds').fill('3')
            page.locator('#session-form button').click()
            expect(page.locator('#job-info')).to_contain_text('等待提案')
            page.locator('#package').click()
            expect(page.locator('#context')).to_have_value(re.compile('.*formula_language.*', re.S))
            context = json.loads(page.locator('#context').input_value())
            assert 'raw_csv' not in json.dumps(context)
            assert context['context']['baseline']['is_demo']
            with page.expect_download() as download: page.locator('#download-package').click()
            assert json.loads(Path(download.value.path()).read_text()) == context
            # Pause remains durable; proposals imported while paused must not start.
            page.locator('#pause').click(); expect(page.locator('#job-info')).to_contain_text('已暂停')
            proposals = [dict(action='propose',name='浏览器验证',expression='momentum(6,0)',rationale='同一公式应该未改善'),
                         dict(action='propose',name='非法提案',expression='__import__("os")',rationale='应拒绝并保留')]
            page.locator('#proposals').fill(json.dumps(proposals,ensure_ascii=False))
            page.locator('#import').click(); expect(page.locator('#status')).to_have_text('操作完成。')
            expect(page.locator('#job-progress')).to_contain_text('轮次 0/3')
            page.locator('#resume').click()
            expect(page.locator('#job-progress')).to_contain_text('轮次 2/3',timeout=15000)
            expect(page.locator('#outcomes')).to_contain_text('未改善')
            expect(page.locator('#outcomes')).to_contain_text('拒绝')
            expect(page.locator('#job-info')).to_contain_text('等待提案')
            page.locator('#package').click()
            expect(page.locator('#context')).to_have_value(re.compile('.*outcomes.*', re.S))
            assert len(json.loads(page.locator('#context').input_value())['progress']['outcomes']) == 2
            page.reload(); expect(page.locator('#jobs')).to_contain_text('等待提案')
            page.locator('#jobs button').first.click(); expect(page.locator('#job-progress')).to_contain_text('轮次 2/3')
            page.locator('#cancel').click(); expect(page.locator('#job-info')).to_contain_text('已取消')
            with page.expect_download() as download: page.locator('#export-job').click()
            record = json.loads(Path(download.value.path()).read_text())
            assert record['status'] == 'cancelled' and len(record['progress']['outcomes']) == 2
            page.locator('#retry').click()
            expect(page.locator('#job-info')).not_to_contain_text(record['id'])
            expect(page.locator('#job-progress')).to_contain_text('轮次 2/3',timeout=15000)
            record2 = json.loads(page.locator('#job-json').text_content())
            assert record2['retry_of'] == record['id'] and record2['id'] != record['id']
            page.screenshot(path='/tmp/quantify-lab-desktop.png', full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
            assert not errors, errors
            browser.close()
            print('PASS: manual session, scoped context export, pause/import/resume, actual experiment, rejected proposal, feedback export, reload, cancel, new retry, mobile; no JS errors')
    finally:
        server.shutdown();server.server_close();thread.join()
        if hasattr(server, 'lab'): server.lab.close()
