import copy
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack,closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timedelta,timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request,urlopen

from app import Handler
from quantify import datasets
from quantify.forward_portfolio import replay,orders_for,observation
from quantify.jobs import JobStore
from quantify.lab import Lab
from quantify.lifecycle import Lifecycle
from quantify.pipeline import PipelineService,PipelineScheduler,append_only
from quantify.portfolio import Account,parameters
from quantify.portfolio_lifecycle import PortfolioLifecycle
from quantify.portfolio_service import PortfolioService
from quantify.workbench import Workbench
from test_portfolio_validation import build
from test_lab import wait_job


def extend(store,bundle,end='2026-07-31'):
    rows=[];states=[];previous={r['symbol']:r['close'] for r in bundle['daily']['rows'] if r['date']==bundle['daily']['date_range'][1]}
    stamp=date.fromisoformat(bundle['daily']['date_range'][1])+timedelta(days=1)
    while stamp.isoformat()<=end:
        if stamp.weekday()<5:
            for s in sorted(previous):
                p=round(previous[s]*(1.0004 if int(s[1:])<2 else .9994),2);previous[s]=p
                rows.append(dict(date=stamp.isoformat(),symbol=s,open=p,high=p,low=p,close=p,volume=1000000,total_return_index='',available_at=stamp.isoformat()))
                states.append(dict(date=stamp.isoformat(),symbol=s,suspended=0,limit_up='',limit_down='',no_price_limit=1,available_at=stamp.isoformat()))
        stamp+=timedelta(days=1)
    d=datasets.import_snapshot(store,kind='daily',raw_csv=datasets.csv_text('daily',rows),source=bundle['daily']['source'],price_basis='unadjusted',is_demo=True,parent_id=bundle['daily']['id'])
    x=datasets.import_snapshot(store,kind='execution',raw_csv=datasets.csv_text('execution',states),source=bundle['states']['source'],price_basis='not_applicable',is_demo=True,parent_id=bundle['states']['id'])
    return d,x


def activate_portfolio(w,bundle,clock):
    p=PortfolioService(w);base=bundle['spec']
    s=p.create_spec(experiment_id=base['experiment_id'],factor_id=base['factor_id'],daily_id=base['daily_id'],execution_id=base['execution_id'],config=dict(rebalance_months=1))
    r=p.run(spec_id=s['id']);l=PortfolioLifecycle(w)
    f=l.freeze(development_run_id=r['id'],objective=dict(min_observed_days=2))
    e=l.evaluate(freeze_id=f['id']);assert e['accepted'],e
    with patch('quantify.portfolio_lifecycle.now',return_value=(clock-timedelta(hours=1)).isoformat()):
        l.change(action='activate',expected_active=None,reason='合成前向验证',freeze_id=f['id'])
    return f


class ForwardReplayTests(unittest.TestCase):
    def test_orders_frozen_and_only_execute_after_actual_generation_date(self):
        config=parameters(dict(initial_cash=10000,top_k=1,cash_buffer=0,slippage_bps=0))
        a=Account(config);orders=orders_for(a,['A'],{'A':10},config,'2025-12-31')
        self.assertEqual(orders[0]['quantity'],1000)
        signal=dict(id='s',valuation_date='2026-01-02',generation_date='2026-01-05',generated_at='2026-01-05T09:00:00+00:00',selected_due=True,orders=dict(selected=orders,reference=[]))
        bars={d:{'A':dict(open=p,close=p)} for d,p in [('2026-01-02',10),('2026-01-05',10),('2026-01-06',20)]}
        states={d:{'A':dict(suspended=0,no_price_limit=1)} for d in bars}
        _,r=replay(signals=[signal],daily=bars,states=states,config=config,reference_config=config)
        self.assertEqual(r['selected']['trades'][0]['date'],'2026-01-06')
        self.assertEqual(r['selected']['orders'][0]['quantity'],1000)
        self.assertEqual(r['selected']['trades'][0]['quantity'],400)
        self.assertEqual(r['selected']['curve'][1]['position_value'],0)
        _,pending=replay(signals=[signal],daily={k:v for k,v in bars.items() if k<'2026-01-06'},states=states,config=config,reference_config=config)
        self.assertFalse(pending['selected']['trades']);self.assertEqual(len(pending['pending_orders']),1)
        self.assertEqual(observation(pending,dict(min_periods=2,min_observed_days=2,min_trades=1))['state'],'insufficient_mature_labels')

    def test_mature_period_degradation_needs_samples_and_ties_are_not_failure(self):
        base=dict(completed_periods=2,observed_days=70,trade_count=2,positive_period_rate=.5,total_return=.1,max_drawdown=-.1,worst_period_return=-.03)
        policy=dict(primary='positive_period_rate',min_periods=2,min_observed_days=60,min_trades=1,max_return_deterioration=0,max_drawdown_deterioration=0,max_worst_period_deterioration=0)
        r=dict(selected_metrics=base,reference_metrics=base,periods=[{},{}])
        self.assertFalse(observation(r,policy)['degradation'])
        r['selected_metrics']=base|dict(positive_period_rate=0,total_return=-.2)
        self.assertTrue(observation(r,policy)['degradation'])
        r['selected_metrics']['completed_periods']=1
        self.assertFalse(observation(r,policy)['degradation'])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.w=Workbench(self.temp.name);self.store=self.w.store;self.bundle=build(self.w)
        self.time=datetime(2026,1,2,9,tzinfo=timezone.utc)
        self.frozen=activate_portfolio(self.w,self.bundle,self.time)
        self.daily,self.execution=extend(self.store,self.bundle)
        self.jobs=JobStore(self.temp.name);self.s=PipelineService(self.w,self.jobs,clock=lambda:self.time)
        self.spec=self.s.create(name='合成组合前向',mode='portfolio',daily_root=self.bundle['daily']['id'],execution_root=self.bundle['states']['id'])['spec']

    def run_at(self,stamp):
        self.time=datetime.fromisoformat(stamp)
        return self.s.run(pipeline_id=self.spec['id'])

    def report(self,result):return self.store.get('portfolio_forward_report',result['output']['portfolio_report_id'])

    def test_temporal_views_real_signal_idempotency_and_monthly_maturity(self):
        first=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(first['status'],'completed',first)
        r=self.report(first);self.assertEqual(len(r['signals']),1);signal=r['signals'][0]
        self.assertEqual(signal['monthly_signal_date'],'2025-12-31');self.assertEqual(signal['valuation_date'],'2026-01-02')
        self.assertFalse(r['report']['selected']['trades']);self.assertEqual(r['observation']['mature_periods'],0)
        self.assertEqual(self.store.get('snapshot',first['view_ids']['daily'])['date_range'][1],'2026-01-02')
        repeated=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(first['output']['portfolio_report_id'],repeated['output']['portfolio_report_id'])
        second=self.run_at('2026-01-05T09:00:00+00:00');r2=self.report(second)
        self.assertEqual(r2['signals'],r['signals']);self.assertTrue(r2['report']['selected']['trades'])
        self.assertTrue(all(t['date']>'2026-01-02' for t in r2['report']['selected']['trades']))
        third=self.run_at('2026-02-02T09:00:00+00:00');r3=self.report(third)
        self.assertEqual(r3['observation']['mature_periods'],1);self.assertFalse(third['degradation'])
        fourth=self.run_at('2026-03-02T09:00:00+00:00');r4=self.report(fourth)
        self.assertEqual(r4['observation']['mature_periods'],2)
        self.assertEqual(len(r4['labels']),2)
        self.assertTrue(all(l['matured_at']<=self.time.isoformat() for l in r4['labels']))
        self.assertEqual(self.s.get('/api/pipelines/reports/'+r['id']),r)
        self.assertEqual(self.s.run(pipeline_id=self.spec['id'])['output']['portfolio_report_id'],fourth['output']['portfolio_report_id'])
        self.assertEqual(r4['report']['selected']['curve'][:len(r2['report']['selected']['curve'])],r2['report']['selected']['curve'])
        self.assertEqual(r4['report']['selected']['trades'][:len(r2['report']['selected']['trades'])],r2['report']['selected']['trades'])
        # New frozen fields do not inherit the final-test positions.
        self.assertEqual(r['report']['selected']['curve'][0]['position_value'],0)
        self.assertNotEqual(r4['id'],r2['id'])

    def test_future_data_and_before_close_do_not_fill_early(self):
        first=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(first['status'],'completed',first)
        early=self.run_at('2026-01-05T04:00:00+00:00');r=self.report(early)
        self.assertFalse(r['report']['selected']['trades']);self.assertEqual(r['report']['selected']['curve'][-1]['date'],'2026-01-02')
        later=self.run_at('2026-01-05T09:00:00+00:00');self.assertTrue(self.report(later)['report']['selected']['trades'])
        self.assertTrue(all(t['date']<='2026-01-05' for t in self.report(later)['report']['selected']['trades']))

    def test_crash_after_signal_commit_reuses_saved_quantities_and_timestamp(self):
        original=self.s.store.put
        def crash(kind,*args,**kwargs):
            if kind=='portfolio_forward_report':raise RuntimeError('受控提交前中断')
            return original(kind,*args,**kwargs)
        with patch.object(self.s.store,'put',side_effect=crash):
            with self.assertRaises(RuntimeError):self.s.run(pipeline_id=self.spec['id'])
        signal=self.store.list('portfolio_forward_signal')[0]
        self.time+=timedelta(days=3)
        observed=next(r for r in self.daily['rows'] if r['date']=='2026-01-02')
        changed=datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',[observed|dict(volume=observed['volume']+1)]),source=self.daily['source'],price_basis='unadjusted',is_demo=True,parent_id=self.daily['id'])
        refused=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(refused['state'],'data_quality')
        self.assertEqual(self.store.list('portfolio_forward_signal'),[signal])
        datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',[observed]),source=self.daily['source'],price_basis='unadjusted',is_demo=True,parent_id=changed['id'])
        restored=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(restored['status'],'completed',restored)
        self.assertEqual(self.report(restored)['signals'],[signal]);self.assertEqual(len(self.store.list('portfolio_forward_signal')),1)
        self.assertEqual(self.report(restored)['report']['selected']['orders'][0]['quantity'],signal['orders']['selected'][0]['quantity'])

    def test_revision_backfill_or_branch_pauses_without_overwriting_head(self):
        good=self.s.run(pipeline_id=self.spec['id']);head=good['output']['portfolio_report_id']
        rows=copy.deepcopy(self.daily['rows']);rows[0]['open']+=.01;rows[0]['high']+=.01
        bad=datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',rows),source=self.daily['source'],price_basis='unadjusted',is_demo=True,parent_id=self.daily['id'])
        failed=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(failed['state'],'data_quality');self.assertFalse(failed['degradation'])
        self.assertEqual(self.s.get('/api/pipelines/portfolio/'+self.spec['id'])['id'],head)
        self.assertEqual(self.s.runtime(self.spec['id'])['blocked'],'data_quality')
        # A second branch is explicitly rejected instead of picking its newest timestamp.
        rows=copy.deepcopy(self.daily['rows']);rows[-1]['volume']+=1
        datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',rows),source=self.daily['source'],price_basis='unadjusted',is_demo=True,parent_id=self.daily['id'])
        ambiguous=self.s.run(pipeline_id=self.spec['id']);self.assertIn('分支',ambiguous['error'])
        old=self.bundle['daily'];new=copy.deepcopy(old);new['rows'].append(old['rows'][0]|dict(symbol='NEW'));new['symbols']=old['symbols']+['NEW']
        with self.assertRaises(ValueError):append_only(old,new)
        old=dict(kind='daily',is_demo=True,price_basis='unadjusted',symbols=['A'],rows=[dict(date='2026-01-01',x=1),dict(date='2026-01-05',x=2)],date_range=['2026-01-01','2026-01-05'])
        new=copy.deepcopy(old);new['rows'].append(dict(date='2026-01-02',x=3))
        with self.assertRaisesRegex(ValueError,'回填'):append_only(old,new)

    def test_version_event_aba_old_engine_and_stale_data_are_not_degradation(self):
        with patch('quantify.portfolio_lifecycle.now',return_value=self.time.isoformat()):
            self.s.portfolio.change(action='retire',expected_active=self.frozen['id'],reason='停用')
            self.s.portfolio.change(action='activate',expected_active=None,freeze_id=self.frozen['id'],reason='重新启用')
        r=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(r['state'],'version_changed');self.assertFalse(r['degradation'])
        with patch('quantify.pipeline.engine_fingerprint',return_value={}):
            r=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(r['state'],'version_changed')
        fresh=self.s.create(name='新生效事件',mode='portfolio',daily_root=self.bundle['daily']['id'],execution_root=self.bundle['states']['id'])['spec']
        self.time=datetime(2026,9,1,9,tzinfo=timezone.utc)
        r=self.s.run(pipeline_id=fresh['id']);self.assertEqual(r['state'],'waiting_data');self.assertFalse(r['degradation'])

    def test_scheduler_opt_in_intents_dedup_and_cancel_block(self):
        scheduler=PipelineScheduler(self.s,start_thread=False);scheduler.tick();self.assertEqual(self.jobs.jobs(),[])
        runtime=self.s.configure(pipeline_id=self.spec['id'],enabled=True,expected_revision=0)
        scheduler.tick();scheduler.tick();self.assertEqual(len(self.jobs.jobs()),1)
        job=self.jobs.jobs()[0];self.jobs.control(job['id'],'cancel')
        self.assertEqual(self.s.runtime(self.spec['id'])['blocked'],'execution_failed')
        scheduler.tick()
        self.assertEqual(self.s.runtime(self.spec['id'])['blocked'],'execution_failed')
        scheduler.tick();self.assertEqual(len(self.jobs.jobs()),1)
        with self.assertRaisesRegex(ValueError,'已改变'):self.s.configure(pipeline_id=self.spec['id'],enabled=False,expected_revision=0)
        self.s.configure(pipeline_id=self.spec['id'],enabled=False,expected_revision=runtime['revision'])

    def test_single_pipeline_joins_signal_and_only_mature_labels(self):
        # Different symbols avoid reusing the portfolio's exposed final interval.
        b=build(self.w,symbols_prefix='S');d,x=extend(self.store,b)
        life=Lifecycle(self.w);f=life.freeze(experiment_id=b['experiment']['id']);e=life.evaluate(freeze_id=f['id']);self.assertTrue(e['accepted'],e)
        with patch('quantify.lifecycle.now',return_value=self.time-timedelta(hours=1)):
            life.change(action='activate',expected_active=None,freeze_id=f['id'],reason='单笔合成')
        s=self.s.create(name='单笔流水线',mode='single',daily_root=b['daily']['id'])['spec']
        with patch('quantify.lifecycle.now',side_effect=lambda:self.time):
            first=self.s.run(pipeline_id=s['id']);self.assertEqual(first['status'],'completed',first)
            signal=self.store.get('forward_signal',first['output']['signal_id']);self.assertEqual(signal['generated_at'],self.time.isoformat())
            self.assertEqual(first['output']['forward']['pending_labels'],1)
            self.time+=timedelta(days=1)
            life.change(action='retire',expected_active=f['id'],reason='测试新事件')
            life.change(action='activate',expected_active=None,freeze_id=f['id'],reason='同版本新启用')
            new=self.s.create(name='新单笔事件',mode='single',daily_root=b['daily']['id'])['spec']
            renewed=self.s.run(pipeline_id=new['id']);self.assertEqual(renewed['status'],'completed',renewed)
            self.assertNotEqual(renewed['output']['signal_id'],signal['id'])
            self.assertEqual(len(renewed['output']['forward']['signals']),1)
            self.assertEqual(self.s.run(pipeline_id=s['id'])['state'],'version_changed')
            self.time=datetime(2026,5,1,9,tzinfo=timezone.utc)
            later=self.s.run(pipeline_id=new['id']);self.assertEqual(later['status'],'completed',later)
            self.assertEqual(later['output']['forward']['evaluated_windows'],1)
            self.assertFalse(later['degradation']);label=self.store.get('forward_label',renewed['output']['signal_id'])
            self.assertGreater(label['entry'],signal['generated_at'][:10]);self.assertLessEqual(label['exit'],'2026-05-01')

    def test_persisted_inputs_reports_and_disabled_state_survive_reopen(self):
        r=self.s.run(pipeline_id=self.spec['id']);reopened=PipelineService(Workbench(self.temp.name),self.jobs,clock=lambda:self.time)
        self.assertFalse(reopened.runtime(self.spec['id'])['enabled'])
        self.assertEqual(reopened.get('/api/pipelines/portfolio/'+self.spec['id']),self.report(r))
        self.assertEqual(reopened.run(pipeline_id=self.spec['id'])['output']['portfolio_report_id'],r['output']['portfolio_report_id'])

    def test_bad_execution_state_halts_and_explicit_valid_run_recovers(self):
        good=self.s.run(pipeline_id=self.spec['id']);self.time=datetime(2026,1,5,9,tzinfo=timezone.utc)
        row=next(r for r in self.execution['rows'] if r['date']=='2026-01-05')
        bad=datasets.import_snapshot(self.store,kind='execution',raw_csv=datasets.csv_text('execution',[row|dict(suspended=1)]),source=self.execution['source'],price_basis='not_applicable',is_demo=True,parent_id=self.execution['id'])
        failed=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(failed['state'],'data_quality');self.assertFalse(failed['degradation'])
        self.assertEqual(self.s.get('/api/pipelines/portfolio/'+self.spec['id'])['id'],good['output']['portfolio_report_id'])
        datasets.import_snapshot(self.store,kind='execution',raw_csv=datasets.csv_text('execution',[row]),source=bad['source'],price_basis='not_applicable',is_demo=True,parent_id=bad['id'])
        recovered=self.s.run(pipeline_id=self.spec['id']);self.assertEqual(recovered['status'],'completed',recovered)
        self.assertIsNone(self.s.runtime(self.spec['id'])['blocked']);self.assertTrue(self.report(recovered)['report']['selected']['trades'])

    def test_concurrent_schedule_and_manual_submit_are_deduplicated(self):
        self.s.configure(pipeline_id=self.spec['id'],enabled=True,expected_revision=0)
        scheduler=PipelineScheduler(self.s,start_thread=False)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:scheduler.tick(),range(2)))
        self.assertEqual(len(self.jobs.jobs()),1)
        self.jobs.control(self.jobs.jobs()[0]['id'],'cancel')
        def submit(_):
            try:return self.s.submit(pipeline_id=self.spec['id'])
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_orphaned_schedule_intent_is_blocked_on_restart(self):
        self.s.configure(pipeline_id=self.spec['id'],enabled=True,expected_revision=0)
        self.assertTrue(self.s.claim_intent(self.spec['id'],'orphan',self.time))
        reopened=PipelineService(Workbench(self.temp.name),self.jobs,clock=lambda:self.time)
        scheduler=PipelineScheduler(reopened,start_thread=False);scheduler.tick()
        self.assertEqual(self.jobs.jobs(),[])
        self.assertEqual(reopened.runtime(self.spec['id'])['blocked'],'execution_failed')


class PipelineHTTPTests(unittest.TestCase):
    def test_actual_child_api_and_explicit_schedule_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            w=Workbench(temp);b=build(w);clock=datetime.now(timezone.utc)
            f=activate_portfolio(w,b,clock)
            # Use only dates declared complete at the real clock; no injected worker clock.
            extend(w.store,b,end=(clock.astimezone(timezone(timedelta(hours=8))).date()-timedelta(days=1)).isoformat())
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp;t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            def api(path,payload=None):
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=30) as r:
                    return json.load(r) if r.headers.get_content_type()=='application/json' else r.read()
            try:
                spec=api('/api/pipelines/specs',dict(name='HTTP组合前向',mode='portfolio',daily_root=b['daily']['id'],execution_root=b['states']['id']))
                self.assertFalse(spec['runtime']['enabled']);j=api('/api/pipelines/run',dict(pipeline_id=spec['spec']['id']))
                done=wait_job(server.lab.store,j['id'],seconds=30);self.assertEqual(done['status'],'succeeded',done)
                result=api('/api/pipelines/runs/'+done['result']['pipeline_run_id']);self.assertEqual(result['status'],'completed',result)
                self.assertTrue(api('/api/pipelines/portfolio/'+spec['spec']['id'])['signals'])
                self.assertEqual(len(api('/api/pipelines/specs')),1)
                self.assertEqual(api('/api/pipelines/config',dict(pipeline_id=spec['spec']['id'],enabled=False,expected_revision=0))['revision'],1)
            finally:
                if hasattr(server,'lab'):server.lab.close()
                server.shutdown();server.server_close();t.join()
