"""Optional end-to-end check using preinstalled Playwright and Chromium; no project dependency.

Run: python tests/browser_workspace.py
All application data are created in a temporary directory.
"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler
from playwright.sync_api import sync_playwright, expect

with tempfile.TemporaryDirectory() as temp:
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.data_dir = temp
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
            page = browser.new_page(viewport={'width':1280,'height':900}, accept_downloads=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{server.server_port}'
            page.goto(base + '/workspace')
            expect(page.locator('#summary')).to_contain_text('5 个因子版本')
            page.locator('#demo').click()
            expect(page.locator('#summary')).to_contain_text('1 个数据快照')
            options = page.locator('#factor-parent option').evaluate_all('(items) => items.map(item => ({value:item.value, text:item.textContent}))')
            momentum = next(item['value'] for item in options if item['text'].startswith('动量'))
            page.locator('#factor-parent').select_option(momentum)
            page.locator('#factor-name').fill('动量12月浏览器验证')
            page.locator('#lookback').fill('12')
            page.locator('#reason').fill('检验更长窗口，所有数据仅用于合成流程测试')
            page.locator('#factor-form button').click()
            expect(page.locator('#summary')).to_contain_text('6 个因子版本')
            page.locator('#factor-options label').filter(has_text='动量12月浏览器验证').locator('input').check()
            page.locator('#run-form button').click()
            expect(page.locator('#summary')).to_contain_text('1 次实验')
            expect(page.locator('#result-note')).to_contain_text('未计算封存期收益')
            expect(page.locator('#result-metrics')).to_contain_text('因子筛选')
            page.locator('#replay').click()
            expect(page.locator('#summary')).to_contain_text('2 次实验')
            choices=page.locator('#compare-right option').evaluate_all('(items)=>items.map(item=>item.value)')
            page.locator('#compare-left').select_option(choices[0])
            page.locator('#compare-right').select_option(choices[1])
            page.locator('#compare-form button').click()
            expect(page.locator('#comparison')).to_contain_text('0.00%')
            with page.expect_download() as download:
                page.locator('#download-run').click()
            exported=json.loads(Path(download.value.path()).read_text())
            assert exported['status']=='completed' and exported['report']['is_demo']
            csv='date,symbol,open,high,low,close,volume,total_return_index,available_at\n2025-01-02,600000.SH,10,11,9,10,10000,,2025-01-02\n'
            page.locator('#is-demo').select_option('true')
            page.locator('#source').fill('浏览器合成日线，元/股，成交量股，无分红指数')
            page.locator('#csv-file').set_input_files({'name':'synthetic.csv','mimeType':'text/csv','buffer':csv.encode()})
            page.locator('#import-form button').click()
            expect(page.locator('#summary')).to_contain_text('2 个数据快照')
            expect(page.locator('#data-meta')).to_contain_text('缺少含分红指数')
            page.reload()
            expect(page.locator('#summary')).to_contain_text('2 次实验')
            expect(page.locator('#summary')).to_contain_text('2 个数据快照')
            page.screenshot(path='/tmp/quantify-workspace-desktop.png', full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
            assert not errors, errors
            page.goto(base + '/')
            expect(page.locator('#stock option')).to_have_count(5)
            expect(page.get_by_role('link',name='进入研究工作台：数据中心 · 因子版本 · 实验记录 →')).to_be_visible()
            assert not errors, errors
            browser.close()
            print('PASS: browser import, version, experiment, replay, comparison, JSON export, reload persistence, mobile width, legacy page; no JS errors')
    finally:
        server.shutdown();server.server_close();thread.join()
