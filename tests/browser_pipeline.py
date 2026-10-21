"""Offline UI acceptance of pipeline binding, real workers, schedule controls and evidence."""
import json
import subprocess
import sys
import tempfile
import threading
from datetime import datetime,timedelta,timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify import datasets
from quantify.jobs import QueueManager
from quantify.lifecycle import Lifecycle
from quantify.workbench import Workbench
from test_pipeline import build,extend,activate_portfolio
from playwright.sync_api import sync_playwright,expect

slow=False
original=QueueManager._spawn

def spawn(manager,job):
    if slow and job['kind']=='pipeline':return subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    return original(manager,job)

with tempfile.TemporaryDirectory() as temp,patch.object(QueueManager,'_spawn',spawn):
    w=Workbench(temp);clock=datetime.now(timezone.utc);bundle=build(w);activate_portfolio(w,bundle,clock)
    end=(clock.astimezone(timezone(timedelta(hours=8))).date()-timedelta(days=1)).isoformat()
    daily,states=extend(w.store,bundle,end=end)
    single=build(w,symbols_prefix='S');extend(w.store,single,end=end)
    life=Lifecycle(w);f=life.freeze(experiment_id=single['experiment']['id']);assert life.evaluate(freeze_id=f['id'])['accepted']
    life.change(action='activate',freeze_id=f['id'],expected_active=None,reason='浏览器合成单笔')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            page=browser.new_page(viewport={'width':1280,'height':900},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url+'/forward');expect(page.locator('#status')).to_have_text('操作完成。')
            page.locator('#name').fill('浏览器组合流水线');page.locator('#daily').select_option(bundle['daily']['id']);page.locator('#execution').select_option(bundle['states']['id'])
            page.locator('#pipeline-form button').click();expect(page.locator('#created')).to_contain_text('自动运行关闭')
            row=page.locator('#pipelines tbody tr').filter(has_text='浏览器组合流水线')
            row.get_by_role('button',name='验证并运行').click();expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=30000)
            expect(page.locator('#result-note')).to_contain_text('成熟样本不足');expect(page.locator('#signals')).to_contain_text('2026-')
            before=json.loads(page.locator('#result-json').text_content());assert len(before['portfolio']['signals'])==1
            assert not before['portfolio']['report']['selected']['trades'];assert before['portfolio']['report']['pending_orders']
            assert before['portfolio']['report']['selected']['curve'][0]['position_value']==0
            with page.expect_download() as download:page.locator('#export').click()
            assert json.loads(Path(download.value.path()).read_text())==before
            row.get_by_role('button',name='开启自动运行').click();expect(row).to_contain_text('已开启')
            row.get_by_role('button',name='关闭自动运行').click();expect(row).to_contain_text('关闭')
            row.get_by_role('button',name='验证并运行').click();expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=30000)
            expect(page.locator('#status')).to_have_text('操作完成。')
            assert len(w.store.list('portfolio_forward_signal'))==1
            # Existing data corrections pause; they must not rewrite the previous forward evidence.
            changed=daily['rows'][0]|dict(volume=daily['rows'][0]['volume']+1)
            datasets.import_snapshot(w.store,kind='daily',raw_csv=datasets.csv_text('daily',[changed]),source=daily['source'],price_basis='unadjusted',is_demo=True,parent_id=daily['id'])
            row.get_by_role('button',name='验证并运行').click();expect(page.locator('#job-info')).to_contain_text('failed',timeout=30000)
            expect(page.locator('#result-note')).to_contain_text('数据质量异常');assert not json.loads(page.locator('#result-json').text_content())['run']['degradation']
            page.locator('#runs tbody tr').filter(has_text=before['run']['id'][:10]).get_by_role('button').click()
            expect(page.locator('#result-json')).to_contain_text(before['portfolio']['id'])
            assert json.loads(page.locator('#result-json').text_content())['portfolio']==before['portfolio']
            slow=True;row.get_by_role('button',name='验证并运行').click();expect(page.locator('#job-info')).to_contain_text('running',timeout=30000)
            page.locator('#cancel').click();expect(page.locator('#job-info')).to_contain_text('cancelled',timeout=30000)
            slow=False;page.locator('#retry').click();expect(page.locator('#job-info')).to_contain_text('failed',timeout=30000)
            # Single-trade pipeline joins the existing signal / maturity service without execution data.
            page.locator('#mode').select_option('single');expect(page.locator('#execution')).to_be_disabled()
            page.locator('#name').fill('浏览器单笔流水线');page.locator('#daily').select_option(single['daily']['id'])
            page.locator('#pipeline-form button').click();expect(page.locator('#created')).to_contain_text('浏览器单笔流水线')
            page.locator('#pipelines tbody tr').filter(has_text='浏览器单笔流水线').get_by_role('button',name='验证并运行').click()
            expect(page.locator('#job-info')).to_contain_text('succeeded',timeout=30000)
            expect(page.locator('#summary')).to_contain_text('等待标签')
            result=json.loads(page.locator('#result-json').text_content());assert result['run']['output']['forward']['pending_labels']==1
            page.reload();expect(page.locator('#status')).to_have_text('操作完成。');expect(page.locator('#pipelines')).to_contain_text('浏览器组合流水线')
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path='/tmp/quantify-013-forward-mobile.png',full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'mobile overflow'
            assert not errors,errors;browser.close()
        print('PASS: portfolio/single pipelines, actual worker, pending orders and real timestamps, opt-in switch, immutable export/history, revision pause, cancel/retry, persistence/mobile')
    finally:
        if hasattr(server,'lab'):server.lab.close()
        server.shutdown();server.server_close();thread.join()
