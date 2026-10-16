"""Optional browser validation: python tests/browser_lifecycle.py."""
import json
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify.workbench import Workbench
from test_lifecycle import import_rows, trend_rows
from playwright.sync_api import sync_playwright, expect

with tempfile.TemporaryDirectory() as temp:
    wb = Workbench(temp)
    factor = next(f for f in wb.store.list('factor') if f['kind'] == 'quality')
    snap = import_rows(wb.store, trend_rows())
    experiment = wb.run(snapshot_id=snap['id'], factor_ids=[factor['id']])
    snap2 = import_rows(wb.store, trend_rows(prefix='OTHER'))
    experiment2 = wb.run(snapshot_id=snap2['id'], factor_ids=[factor['id']])
    new = import_rows(wb.store, trend_rows(118))  # Latest October 2026; entry November, not yet realized.
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler); server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            base=f'http://127.0.0.1:{server.server_port}'
            page.goto(base+'/strategies')
            expect(page.locator('#experiment option')).to_have_count(2)
            page.select_option('#experiment',experiment['id'])
            page.fill('#name','首个合成研究版本')
            page.locator('#freeze-form button').click()
            expect(page.locator('#versions')).to_contain_text('首个合成研究版本')
            expect(page.locator('#result-json')).to_contain_text('首个合成研究版本')
            expect(page.locator('#status')).to_have_text('操作完成。')
            frozen=json.loads(page.locator('#result-json').text_content())['frozen']
            first=frozen['id']
            page.get_by_role('button',name='最终验收',exact=True).click()
            expect(page.locator('#versions')).to_contain_text('通过（研究用途）')
            expect(page.locator('#status')).to_have_text('操作完成。')
            result=json.loads(page.locator('#result-json').text_content())['evaluation']
            assert result['accepted'] and result['independent'] and result['is_demo']
            with page.expect_download() as download: page.locator('#export').click()
            exported=json.loads(Path(download.value.path()).read_text())
            assert exported['evaluation']==result
            page.fill('#reason','浏览器人工启用');page.locator('#change-form button').click()
            expect(page.locator('#active')).to_contain_text(first)
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.select_option('#snapshot',new['id']);page.locator('#signal').click()
            expect(page.locator('#forward-note')).to_contain_text('等待成熟 1')
            page.locator('#observe').click()
            expect(page.locator('#forward-note')).to_contain_text('成熟样本不足')
            expect(page.locator('#status')).to_have_text('操作完成。')
            report=json.loads(page.locator('#forward-json').text_content())
            assert len(report['signals'])==1 and not report['labels'] and report['is_demo']
            page.select_option('#experiment',experiment2['id']);page.fill('#name','第二个合成研究版本')
            page.locator('#freeze-form button').click()
            expect(page.locator('#versions tbody tr')).to_have_count(2)
            expect(page.locator('#result-json')).to_contain_text('第二个合成研究版本')
            expect(page.locator('#status')).to_have_text('操作完成。')
            second=json.loads(page.locator('#result-json').text_content())['frozen']['id']
            page.locator('#versions tbody tr').filter(has_text='第二个合成研究版本').get_by_role('button',name='最终验收',exact=True).click()
            expect(page.locator('#target option')).to_have_count(2)
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.select_option('#target',second);page.fill('#reason','替换为第二版');page.locator('#change-form button').click()
            expect(page.locator('#active')).to_contain_text(second)
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.select_option('#operation','rollback');page.select_option('#target',first)
            page.fill('#reason','回滚到第一版');page.locator('#change-form button').click()
            expect(page.locator('#active')).to_contain_text(first)
            expect(page.locator('#events tbody tr')).to_have_count(3)
            expect(page.locator('#status')).to_have_text('操作完成。')
            page.select_option('#operation','retire');page.fill('#reason','退役');page.locator('#change-form button').click()
            expect(page.locator('#active')).to_have_text('当前没有启用研究版本')
            expect(page.locator('#events tbody tr')).to_have_count(4)
            page.reload();expect(page.locator('#events tbody tr')).to_have_count(4)
            page.screenshot(path='/tmp/quantify-lifecycle-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'mobile overflow'
            assert not errors, errors
            browser.close()
            print('PASS: freeze, independent final evaluation, export, manual activation/replacement/rollback/retire, future signal, immature labels, persisted events, mobile; no JS errors')
    finally:
        server.shutdown();server.server_close();thread.join()
        if hasattr(server,'lab'):server.lab.close()
