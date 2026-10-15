"""Browser acceptance with explicitly synthetic upstream fixtures, never a live-source claim."""
import json
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify import datasets
from quantify.jobs import QueueManager
from quantify.lab import Lab
from quantify.market_providers import CHINA, ProviderError, parse_sina
from test_market import sina_row
from test_workspace import daily_csv
from playwright.sync_api import sync_playwright, expect

NOW = datetime(2026, 10, 15, 10, 0, 0, tzinfo=CHINA)

class QuoteFixture:
    fail = False
    def quotes(self, symbols):
        if self.fail: raise ProviderError('network_policy', '合成故障：代理拒绝建立连接')
        raw = b''.join(sina_row(symbol[-2:].lower()+symbol[:6]) for symbol in symbols)
        return parse_sina(raw, symbols, NOW)

original_spawn = QueueManager._spawn

def spawn(manager, job):
    if job['kind'] == 'history':
        # Verify HTTP/UI submission and actual process cancellation without calling upstream.
        return subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
    return original_spawn(manager, job)

with tempfile.TemporaryDirectory() as temp, patch.object(QueueManager, '_spawn', spawn):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.data_dir = temp
    server.lab = Lab(temp)
    fixture = QuoteFixture(); server.lab.market.provider = fixture; server.lab.market.clock = lambda: NOW
    daily = datasets.import_snapshot(server.lab.store, kind='daily', raw_csv=daily_csv(108, tri=False),
               source='浏览器合成价格数据；无分红指数', price_basis='unadjusted', is_demo=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
            page = browser.new_page(viewport={'width':1280,'height':900})
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            base = f'http://127.0.0.1:{server.server_port}'
            page.goto(base + '/market')
            expect(page.locator('#symbols')).to_have_value('000001.SZ, 600519.SH')
            expect(page.locator('#quotes')).to_contain_text('暂无报价')
            page.locator('#symbols').fill('600519.SH, 000001.SZ')
            page.locator('#history-symbols').fill('600000.SH, 600001.SH, 600002.SH')
            page.locator('#config-form button').click(); expect(page.locator('#status')).to_have_text('操作完成。')
            page.locator('#refresh-quotes').click()
            expect(page.locator('#quotes')).to_contain_text('合成接口测试', timeout=10000)
            expect(page.locator('#quotes')).to_contain_text('2026-10-15T10:00:00+08:00')
            page.locator('#history-end').fill('2026-10-14')
            page.locator('#download').click(); expect(page.locator('#history-info')).to_contain_text('任务')
            page.goto(base + '/lab')
            expect(page.locator('#jobs')).to_contain_text('历史日线下载')
            page.locator('#jobs button').first.click()
            expect(page.locator('#job-progress')).to_contain_text('历史下载')
            page.locator('#cancel').click(); expect(page.locator('#job-info')).to_contain_text('已取消', timeout=10000)
            page.goto(base + '/workspace')
            expect(page.locator('#summary')).to_contain_text('1 个数据快照')
            page.locator('details').filter(has=page.locator('#monthly-form')).locator('summary').click()
            page.locator('#return-basis').select_option('price')
            page.locator('#monthly-form button').click()
            expect(page.locator('#summary')).to_contain_text('2 个数据快照')
            page.locator('#factor-options label').filter(has_text='动量').locator('input').check()
            page.locator('#run-form button').click()
            expect(page.locator('#result-note')).to_contain_text('不含现金分红')
            expect(page.locator('#result-metrics')).to_contain_text('因子筛选')
            page.goto(base + '/lab')
            expect(page.locator('#baseline option')).to_have_count(1)
            page.locator('#session-form button').click()
            expect(page.locator('#job-info')).to_contain_text('等待提案')
            page.locator('#package').click()
            expect(page.locator('#context')).to_have_value(re.compile('.*"return_basis": "price".*', re.S))
            package = json.loads(page.locator('#context').input_value())
            assert package['context']['data']['available_fields'] == ['close']
            assert 'raw_csv' not in json.dumps(package)
            # Configuration and latest cached quote survive reload; a later failed query is explicit.
            page.goto(base + '/market')
            expect(page.locator('#history-symbols')).to_have_value('600000.SH, 600001.SH, 600002.SH')
            fixture.fail = True
            page.wait_for_timeout(5100)
            page.locator('#refresh-quotes').click()
            expect(page.locator('#quotes')).to_contain_text('获取失败', timeout=10000)
            expect(page.locator('#quotes')).to_contain_text('合成故障')
            expect(page.locator('#quotes')).to_contain_text('合成接口测试')
            page.screenshot(path='/tmp/quantify-market-desktop.png', full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
            assert not errors, errors
            browser.close()
            print('PASS: market configuration, quote timestamp/cache/failure, history queue cancellation, monthly PRICE experiment, AI context basis, reload and mobile; upstream data are synthetic fixtures')
    finally:
        server.shutdown();server.server_close();thread.join();server.lab.close()
