"""Deterministic delayed-response checks; synthetic task replies, real page scripts."""
import json
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Handler
from quantify import factors
from quantify.portfolio_service import PortfolioService
from quantify.workbench import Workbench
from playwright.sync_api import sync_playwright,expect


def answer(route,data):route.fulfill(status=200,content_type='application/json',body=json.dumps(data))

def wait_held(page,held):
    deadline=time.monotonic()+5
    while not held and time.monotonic()<deadline:page.wait_for_timeout(25)
    assert held,'delayed request not issued'


failures=[]
with tempfile.TemporaryDirectory() as temp:
    wb=Workbench(temp);bundle=PortfolioService(wb).demo()
    parent=wb.store.get('factor',bundle['spec']['factor_id'])
    constant=factors.create_version(wb.store,parent_id=parent['id'],name='保留选择',reason='竞态测试',expression='1')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
            base=f'http://127.0.0.1:{server.server_port}'
            for name,scenario in [(name,scenario) for name in ('diagnostics','portfolio') for scenario in ('switch','cancel')]:
                page=browser.new_page();held=[];posts=[];gets={};cancelled=[];errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                def task(i,status='queued'):
                    return dict(id=i,status=status,progress={},error=None,result=None,stop_requested=None)
                def submit(route):
                    i=('a' if not posts else 'b')*32;posts.append(i);answer(route,task(i))
                def poll(route):
                    i=route.request.url.rsplit('/',1)[1];gets[i]=gets.get(i,0)+1
                    if i=='a'*32 and gets[i]==2:held.append(route);return
                    answer(route,task(i,'cancelled' if i in cancelled else 'queued'))
                def control(route):
                    payload=route.request.post_data_json;cancelled.append(payload['job_id']);answer(route,task(payload['job_id'],'cancelled'))
                page.route('**/api/'+('diagnostics/reports' if name=='diagnostics' else 'portfolio/runs'),lambda route:submit(route) if route.request.method=='POST' else route.continue_())
                page.route('**/api/lab/jobs/*',poll);page.route('**/api/lab/control',control)
                try:
                    page.goto(base+'/'+name);expect(page.locator('#status')).to_have_text('操作完成。')
                    trigger=page.locator('#diagnostic-form button') if name=='diagnostics' else page.get_by_role('button',name='提交回放',exact=True)
                    trigger.click();expect(page.locator('#status')).to_have_text('操作完成。');wait_held(page,held)
                    if scenario=='switch':
                        trigger.click();expect(page.locator('#status')).to_have_text('操作完成。')
                        expect(page.locator('#job-info')).to_contain_text('b'*32)
                        answer(held.pop(),task('a'*32,'succeeded'))
                        page.wait_for_timeout(650)
                        expect(page.locator('#job-info')).to_contain_text('b'*32,timeout=1000)
                        page.locator('#cancel').click();expect(page.locator('#job-info')).to_contain_text('cancelled')
                        assert cancelled==['b'*32],cancelled
                    else:
                        page.locator('#cancel').click();expect(page.locator('#job-info')).to_contain_text('cancelled')
                        answer(held.pop(),task('a'*32,'queued'));page.wait_for_timeout(650)
                        expect(page.locator('#job-info')).to_contain_text('cancelled',timeout=1000)
                        expect(page.locator('#cancel')).to_be_disabled()
                    assert not errors,errors
                    print('PASS:',name,scenario,'stale task/status response ignored')
                except Exception as e:failures.append(name+' task race: '+str(e).splitlines()[0])
                finally:page.close()
            page=browser.new_page();held=[];cancelled=[];reads=[0]
            try:
                def lab_task(status='queued'):
                    return dict(id='c'*32,kind='experiment',goal='合成竞态',status=status,progress={},payload={},error=None,result=None,stop_requested=None,elapsed_seconds=0,timeout_seconds=30)
                def lab_poll(route):
                    reads[0]+=1
                    if reads[0]==2:held.append(route);return
                    answer(route,lab_task('cancelled' if cancelled else 'queued'))
                def lab_control(route):
                    cancelled.append(route.request.post_data_json['job_id']);answer(route,lab_task('cancelled'))
                page.route('**/api/lab/jobs',lambda r:answer(r,[lab_task('cancelled' if cancelled else 'queued')]))
                page.route('**/api/lab/jobs/*',lab_poll);page.route('**/api/lab/control',lab_control)
                page.goto(base+'/lab');expect(page.locator('#status')).to_have_text('操作完成。')
                page.get_by_role('button',name='查看',exact=True).click();wait_held(page,held)
                page.locator('#cancel').click();expect(page.locator('#job-info')).to_contain_text('已取消')
                answer(held.pop(),lab_task('queued'));page.wait_for_timeout(250)
                expect(page.locator('#job-info')).to_contain_text('已取消',timeout=500)
                expect(page.locator('#cancel')).to_be_disabled()
                print('PASS: lab old same-task response cannot undo cancelled view')
            except Exception as e:failures.append('lab status race: '+str(e).splitlines()[0])
            finally:page.close()
            page=browser.new_page()
            try:
                page.goto(base+'/diagnostics');expect(page.locator('#status')).to_have_text('操作完成。')
                extra=page.locator('#diagnostic-factors input[value="'+constant['id']+'"]');extra.check()
                page.locator('#refresh').click();expect(page.locator('#status')).to_have_text('操作完成。')
                expect(extra).to_be_checked(timeout=1000)
                print('PASS: diagnostic factor selection survives refresh')
            except Exception as e:failures.append('factor refresh: '+str(e).splitlines()[0])
            finally:page.close()
            page=browser.new_page();held=[]
            try:
                def census(route):
                    scoped='/trials/' in route.request.url
                    result=dict(as_of='SCOPED' if scoped else 'ALL',scope='same_protocol' if scoped else 'local_directory',counts=dict(experiments=0,experiment_statuses={},repeated_experiments=0,sessions=0,proposals_consumed=0,unique_valid_formulas=0,invalid_formulas=0,repeated_proposals=0,stop_proposals=0,proposal_statuses={},submitted_unconsumed=0),note='synthetic race',repeated_formula_groups=[],repeated_experiment_groups=[])
                    if scoped:held.append((route,result));return
                    answer(route,result)
                page.route('**/api/diagnostics/trials**',census)
                page.goto(base+'/diagnostics');expect(page.locator('#trial-note')).to_contain_text('ALL')
                page.select_option('#trial-scope','experiment');wait_held(page,held)
                page.select_option('#trial-scope','all')
                route,result=held.pop();answer(route,result);page.wait_for_timeout(250)
                expect(page.locator('#trial-note')).to_contain_text('ALL',timeout=1000)
                with page.expect_download() as d:page.locator('#export-trials').click()
                assert json.loads(Path(d.value.path()).read_text())['scope']=='local_directory'
                print('PASS: delayed census cannot mismatch selected scope or JSON export')
            except Exception as e:failures.append('trial scope race: '+str(e).splitlines()[0])
            finally:page.close()
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
        if hasattr(server,'lab'):server.lab.close()
if failures:
    for f in failures:print('FAIL:',f)
    raise SystemExit(1)
