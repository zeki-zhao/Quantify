import copy
import json
import math
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Handler
from quantify import datasets
from quantify.exposures import reserve
from quantify.lab import Lab
from quantify.lifecycle import Lifecycle
from quantify.portfolio_lifecycle import PortfolioLifecycle
from quantify.portfolio_policy import assess, policy, summarize
from quantify.portfolio_service import PortfolioService
from quantify.workbench import Workbench
from test_lab import wait_job


@lru_cache(maxsize=1)
def trend_data():
    rows=[];states=[];stamp=date(2017,1,2);step=0
    while stamp<=date(2025,12,31):
        if stamp.weekday()<5:
            for i in range(5):
                p=round((20+i*5)*math.exp((.0004 if i<2 else -.0006)*step),2)
                rows.append(dict(date=stamp.isoformat(),symbol='V'+str(i),open=p,high=p,low=p,close=p,volume=1000000,total_return_index='',available_at=stamp.isoformat()))
                states.append(dict(date=stamp.isoformat(),symbol='V'+str(i),suspended=0,limit_up='',limit_down='',no_price_limit=1,available_at=stamp.isoformat()))
            step+=1
        stamp+=timedelta(days=1)
    return rows,states


def build(w,*,symbols_prefix='V'):
    daily,states=copy.deepcopy(trend_data())
    if symbols_prefix!='V':
        for rows in (daily,states):
            for r in rows:r['symbol']=symbols_prefix+r['symbol'][1:]
    d=datasets.import_snapshot(w.store,kind='daily',raw_csv=datasets.csv_text('daily',daily),source='明确合成单调行情',price_basis='unadjusted',is_demo=True)
    x=datasets.import_snapshot(w.store,kind='execution',raw_csv=datasets.csv_text('execution',states),source='明确合成不限价状态',price_basis='not_applicable',is_demo=True)
    m=datasets.monthly_from_daily(w.store,daily_id=d['id'],return_basis='price')
    factor=next(f for f in w.store.list('factor') if f['kind']=='momentum' and not f['parent_id'])
    e=w.run(snapshot_id=m['id'],factor_ids=[factor['id']])
    p=PortfolioService(w);s=p.create_spec(experiment_id=e['id'],factor_id=factor['id'],daily_id=d['id'],execution_id=x['id'])
    r=p.run(spec_id=s['id']);assert r['status']=='completed',r
    return dict(daily=d,states=x,monthly=m,experiment=e,spec=s,run=r)


class PortfolioPolicyTests(unittest.TestCase):
    def test_net_nav_periods_use_fixed_endpoints_and_exclude_partial_tail(self):
        account=dict(summary=dict(total_return=.3),curve=[dict(date=d,equity=e) for d,e in [('a',100),('b',110),('c',99),('d',130)]])
        metrics,periods=summarize(account,['a','b','c'])
        self.assertEqual(metrics['completed_periods'],2);self.assertEqual(metrics['observed_days'],4)
        self.assertEqual(metrics['positive_period_rate'],.5);self.assertAlmostEqual(metrics['worst_period_return'],-.1)
        self.assertEqual(metrics['total_return'],.3);self.assertEqual([p['end'] for p in periods],['b','c'])
        with self.assertRaises(ValueError):summarize(account,['a','missing'])

    def test_portfolio_policy_strict_goal_guards_and_sample_counts(self):
        base=dict(total_return=.1,max_drawdown=-.1,worst_period_return=-.03,positive_period_rate=.5,completed_periods=3,observed_days=200,trade_count=3)
        good=base|dict(total_return=.2,max_drawdown=-.08,worst_period_return=-.01,positive_period_rate=2/3)
        self.assertTrue(assess(good,base,None)['accepted'])
        for changes,key in [(dict(positive_period_rate=.5),'primary_improved'),(dict(total_return=.05),'return_guard'),
                            (dict(max_drawdown=-.2),'drawdown_guard'),(dict(worst_period_return=-.1),'period_tail_guard'),
                            (dict(trade_count=0),'trade_floor'),(dict(completed_periods=1),'period_floor'),(dict(observed_days=1),'observed_day_floor')]:
            d=assess(good|changes,base,None);self.assertFalse(d['accepted']);self.assertFalse(d['checks'][key])
        self.assertTrue(assess(good|dict(positive_period_rate=.5),base,dict(primary='total_return'))['accepted'])
        for value in ({'primary':'positive_rate'},{'min_periods':1},{'min_observed_days':True},{'max_drawdown_deterioration':float('nan')}):
            with self.assertRaises(ValueError):policy(value)


class PortfolioValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.w=Workbench(self.temp.name);self.store=self.w.store;self.bundle=build(self.w)
        self.p=PortfolioService(self.w);self.l=PortfolioLifecycle(self.w)

    def freeze(self,**changes):
        return self.l.freeze(development_run_id=self.bundle['run']['id'],**changes)

    def test_frozen_final_cash_start_boundaries_fixed_rules_and_cache(self):
        f=self.freeze();r=self.l.evaluate(freeze_id=f['id']);self.assertEqual(r['status'],'completed',r.get('error'))
        self.assertTrue(r['independent']);self.assertTrue(r['accepted'],r['decision'])
        report=r['report'];self.assertEqual(report['selected']['curve'][0]['equity'],100000)
        self.assertEqual(report['selected']['curve'][0]['cash'],100000);self.assertEqual(report['selected']['curve'][0]['position_value'],0)
        self.assertEqual(report['selected']['curve'][0]['date'],f['start']);self.assertEqual(report['selected']['curve'][-1]['date'],f['end'])
        self.assertTrue(all(t['date']>f['start'] for t in report['selected']['trades']))
        self.assertTrue(all(d['date']>=f['start'] for d in report['decisions']))
        self.assertEqual(report['protocol']['period_boundaries'],f['period_boundaries'])
        self.assertEqual(len(report['periods']),len(f['period_boundaries'])-1)
        self.assertEqual(r,self.l.evaluate(freeze_id=f['id']))
        self.assertEqual(self.l.state()['active_id'],None)
        self.assertTrue(report['selected']['holdings'])

    def test_final_reads_holdout_but_ignores_daily_rows_after_frozen_end(self):
        f=self.freeze();first=self.l.evaluate(freeze_id=f['id'])
        # Modifications after the monthly end cannot enter final execution or valuation.
        spec=copy.deepcopy(f['spec']);d=self.store.get('snapshot',spec['daily_id']);x=self.store.get('snapshot',spec['execution_id'])
        for row in d['rows']:
            if row['date']>f['end']:
                for k in ('open','high','low','close'):row[k]*=2
        d2=datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',d['rows']),source='后端点哨兵',price_basis='unadjusted',is_demo=True)
        spec['daily_id']=d2['id']
        dates,panel,bars,states,factor,signals,*_=self.p._inputs(spec,window_start=f['start'],window_end=f['end'])
        self.assertEqual(max(bars),f['end']);self.assertTrue(all(stamp<=f['end'] for stamp in bars))
        from quantify.portfolio import simulate
        r=simulate(dates=dates,panel=panel,daily=bars,states=states,factor=factor,config=spec['config'],signal_dates=signals)
        self.assertEqual(first['report']['selected'],r['selected'])
        # Final-period bar mutations DO affect validation (unlike development replay).
        modified=copy.deepcopy(self.bundle['daily']['rows']);stamp=f['period_boundaries'][0]
        target=next(row for row in modified if row['date']>stamp and row['symbol']=='V0');target['open']=round(target['open']*.5,2);target['low']=target['open']
        dm=datasets.import_snapshot(self.store,kind='daily',raw_csv=datasets.csv_text('daily',modified),source='留出执行哨兵',price_basis='unadjusted',is_demo=True)
        sm=self.p.create_spec(experiment_id=spec['experiment_id'],factor_id=spec['factor_id'],daily_id=dm['id'],execution_id=spec['execution_id'])
        dev=self.p.run(spec_id=sm['id']);self.assertEqual(dev['report'],self.bundle['run']['report'])
        fm=self.l.freeze(development_run_id=dev['id']);changed=self.l.evaluate(freeze_id=fm['id'])
        self.assertFalse(changed['independent']);self.assertNotEqual(changed['report']['selected'],first['report']['selected'])

    def test_shared_exposure_both_directions_and_changed_protocols_do_not_reset(self):
        f=self.freeze();self.l.evaluate(freeze_id=f['id'])
        single=Lifecycle(self.w);s=single.freeze(experiment_id=self.bundle['experiment']['id'])
        e=single.evaluate(freeze_id=s['id']);self.assertFalse(e['independent']);self.assertIn('portfolio:'+f['id'],e['previous_exposures'])
        other=self.freeze(name='换名称和目标',objective=dict(primary='total_return'))
        e=self.l.evaluate(freeze_id=other['id']);self.assertFalse(e['independent']);self.assertFalse(e['accepted'])
        with self.assertRaises(ValueError):self.l.change(action='activate',freeze_id=other['id'],expected_active=None,reason='不得启用')
        # New universe: single-trade reservation first must also taint portfolio validation.
        b=build(self.w,symbols_prefix='N');sf=single.freeze(experiment_id=b['experiment']['id']);single.evaluate(freeze_id=sf['id'])
        pf=self.l.freeze(development_run_id=b['run']['id']);pe=self.l.evaluate(freeze_id=pf['id'])
        self.assertFalse(pe['independent']);self.assertIn(sf['id'],pe['previous_exposures'])

    def test_known_development_overlap_cannot_be_called_independent(self):
        # Move an older final period into a new development window on the same symbols.
        f=self.freeze()
        snap=self.bundle['monthly'];rows=copy.deepcopy(snap['rows']);last=sorted({r['date'] for r in rows})[-1]
        last_rows=[r for r in rows if r['date']==last]
        year,month=int(last[:4]),int(last[5:7])
        for offset in range(1,13):
            absolute=year*12+month-1+offset;stamp=f'{absolute//12}-{absolute%12+1:02d}-28'
            rows.extend([r|dict(date=stamp,available_at=stamp) for r in last_rows])
        m=datasets.import_snapshot(self.store,kind='monthly_price',raw_csv=datasets.csv_text('monthly_price',rows),source='仅软件暴露测试',price_basis='unadjusted',is_demo=True)
        e=self.w.run(snapshot_id=m['id'],factor_ids=[self.bundle['spec']['factor_id']]);self.assertEqual(e['status'],'completed')
        r=self.l.evaluate(freeze_id=f['id']);self.assertFalse(r['independent'])
        self.assertIn('development:'+e['id'],r['previous_exposures'])

    def test_failure_and_crash_reservations_remain_consumed(self):
        f=self.freeze()
        with patch('quantify.portfolio_lifecycle.simulate',side_effect=ValueError('受控失败')):
            r=self.l.evaluate(freeze_id=f['id'])
        self.assertEqual(r['status'],'failed');self.assertEqual(r,self.l.evaluate(freeze_id=f['id']))
        later=self.freeze(name='失败后另一冻结');self.assertFalse(self.l.evaluate(freeze_id=later['id'])['independent'])
        crashed=self.freeze(name='模拟进程崩溃');reserve(self.store,'portfolio:'+crashed['id'],crashed['start'],crashed['end'],crashed['symbols'])
        with self.assertRaisesRegex(ValueError,'已开始验收'):self.l.evaluate(freeze_id=crashed['id'])

    def test_concurrent_freezes_share_one_independent_exposure(self):
        freezes=[self.freeze(name='并发'+str(i)) for i in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda f:self.l.evaluate(freeze_id=f['id']),freezes))
        self.assertEqual(sum(r['independent'] for r in results),1)
        self.assertEqual(sum(r['accepted'] for r in results),1)

    def test_reference_spec_fixed_and_periods_align(self):
        original=self.bundle['spec']
        ref=self.p.create_spec(experiment_id=original['experiment_id'],factor_id=original['factor_id'],daily_id=original['daily_id'],execution_id=original['execution_id'],config=dict(top_k=3,rebalance_months=1))
        f=self.freeze(reference_spec_id=ref['id']);r=self.l.evaluate(freeze_id=f['id'])
        self.assertEqual(r['status'],'completed',r.get('error'));self.assertEqual(r['report']['protocol']['reference'],'fixed_portfolio')
        self.assertEqual(r['report']['selected_metrics']['completed_periods'],r['report']['reference_metrics']['completed_periods'])
        self.assertNotEqual(r['report']['reference']['summary'],r['report']['pool']['summary'])
        wrong=self.p.create_spec(experiment_id=original['experiment_id'],factor_id=original['factor_id'],daily_id=original['daily_id'],execution_id=original['execution_id'],config=dict(initial_cash=50000))
        with self.assertRaisesRegex(ValueError,'资金和费用'):self.freeze(reference_spec_id=wrong['id'])
        too_long=self.p.create_spec(experiment_id=original['experiment_id'],factor_id=original['factor_id'],daily_id=original['daily_id'],execution_id=original['execution_id'],config=dict(rebalance_months=12))
        run=self.p.run(spec_id=too_long['id'])
        with self.assertRaisesRegex(ValueError,'完整调仓周期'):self.l.freeze(development_run_id=run['id'])

    def test_manual_activation_replacement_rollback_conflicts_and_persistence(self):
        f=self.freeze();self.l.evaluate(freeze_id=f['id'])
        with self.assertRaisesRegex(ValueError,'曾经启用'):self.l.change(action='rollback',freeze_id=f['id'],expected_active=None,reason='尚未启用')
        self.l.change(action='activate',freeze_id=f['id'],expected_active=None,reason='合成准入通过')
        other=build(self.w,symbols_prefix='R');new=self.l.freeze(development_run_id=other['run']['id']);self.assertTrue(self.l.evaluate(freeze_id=new['id'])['accepted'])
        with self.assertRaisesRegex(ValueError,'已改变'):self.l.change(action='replace',freeze_id=new['id'],expected_active=None,reason='陈旧页面')
        self.l.change(action='replace',freeze_id=new['id'],expected_active=f['id'],reason='人工替换')
        self.l.change(action='rollback',freeze_id=f['id'],expected_active=new['id'],reason='人工回滚')
        state=self.l.change(action='retire',expected_active=f['id'],reason='结束观察')
        self.assertEqual([e['action'] for e in state['events']],['activate','replace','rollback','retire'])
        self.assertEqual(state,PortfolioLifecycle(Workbench(self.temp.name)).state())
        self.assertEqual(self.store.get('portfolio_run',self.bundle['run']['id']),self.bundle['run'])

    def test_queued_real_worker_cached_replay_and_interrupted_retry(self):
        f=self.freeze();lab=Lab(self.temp.name)
        try:
            j=lab.portfolio_lifecycle.submit(freeze_id=f['id']);done=wait_job(lab.store,j['id'])
            self.assertEqual(done['status'],'succeeded',done.get('error'))
            replay=lab.portfolio_lifecycle.submit(freeze_id=f['id']);again=wait_job(lab.store,replay['id'])
            self.assertTrue(again['progress']['cached']);self.assertEqual(done['result'],again['result'])
            self.assertEqual(lab.portfolio_lifecycle.change(action='activate',freeze_id=f['id'],expected_active=None,reason='任务成功后启用')['active_id'],f['id'])
        finally:lab.close()
        f2=self.freeze(name='已预留后重启');reserve(self.store,'portfolio:'+f2['id'],f2['start'],f2['end'],f2['symbols'])
        pending=lab.portfolio_lifecycle.submit(freeze_id=f2['id'])
        reopened=Lab(self.temp.name)
        try:
            self.assertEqual(reopened.store.job(pending['id'])['status'],'interrupted')
            retried=reopened.retry(pending['id']);done=wait_job(reopened.store,retried['id'])
            self.assertEqual(done['status'],'failed');self.assertIn('已开始验收',done['error'])
        finally:reopened.close()

    def test_old_engine_rejected_and_final_results_not_in_research_context(self):
        f=self.freeze();self.l.evaluate(freeze_id=f['id'])
        with patch('quantify.portfolio_lifecycle.engine_fingerprint',return_value={}):
            with self.assertRaisesRegex(ValueError,'引擎'):self.l.evaluate(freeze_id=f['id'])
            with self.assertRaisesRegex(ValueError,'旧引擎'):self.l.change(action='activate',freeze_id=f['id'],expected_active=None,reason='拒绝旧引擎')
        lab=Lab(self.temp.name)
        try:
            j=lab.create_session(baseline_id=self.bundle['experiment']['id'],goal='只看开发数据')
            package=json.dumps(lab.research_package(j['id']))
            self.assertNotIn('portfolio_evaluation',package);self.assertNotIn(f['id'],package)
            self.assertNotIn('selected_metrics',package)
        finally:lab.close()

    def test_completed_report_from_cancelled_job_requires_successful_cache_delivery(self):
        f=self.freeze();lab=Lab(self.temp.name);lab.close()
        j=lab.portfolio_lifecycle.submit(freeze_id=f['id']);claimed=lab.store.claim('fixture')
        r=lab.portfolio_lifecycle.evaluate(freeze_id=f['id'],job_id=j['id'])
        self.assertTrue(r['accepted'])
        lab.store.control(j['id'],'cancel')
        lab.store.finish(j['id'],'fixture','succeeded',result=dict(portfolio_evaluation_id=f['id']))
        self.assertEqual(lab.store.job(j['id'])['status'],'cancelled')
        with self.assertRaisesRegex(ValueError,'任务尚未成功'):
            lab.portfolio_lifecycle.change(action='activate',freeze_id=f['id'],expected_active=None,reason='不能绕过取消')
        retried=lab.retry(j['id']);lab.store.claim('retry')
        from quantify.worker import execute
        execute(self.temp.name,retried['id'],'retry')
        self.assertEqual(lab.store.job(retried['id'])['status'],'succeeded')
        self.assertTrue(lab.store.job(retried['id'])['progress']['cached'])
        self.assertEqual(lab.portfolio_lifecycle.change(action='activate',freeze_id=f['id'],expected_active=None,reason='完整缓存成功交付后')['active_id'],f['id'])


class PortfolioValidationHTTPTests(unittest.TestCase):
    def test_http_freeze_job_result_activation_and_no_single_trade_shortcut(self):
        with tempfile.TemporaryDirectory() as temp:
            b=build(Workbench(temp));server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
            t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            def api(path,payload=None):
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=20) as r:
                    return json.load(r) if r.headers.get_content_type()=='application/json' else r.read()
            try:
                self.assertTrue(api('/portfolio-validation'));self.assertTrue(api('/portfolio-validation.js'))
                f=api('/api/portfolio-validation/freeze',dict(development_run_id=b['run']['id']))
                single=api('/api/strategies/freeze',dict(experiment_id=b['experiment']['id']))
                with self.assertRaises(HTTPError) as shortcut:
                    api('/api/portfolio-validation/change',dict(action='activate',expected_active=None,reason='单笔资格不得替代组合',freeze_id=single['id']))
                self.assertEqual(shortcut.exception.code,400)
                j=api('/api/portfolio-validation/evaluate',dict(freeze_id=f['id']));done=wait_job(server.lab.store,j['id'])
                self.assertEqual(done['status'],'succeeded',done.get('error'))
                e=api('/api/portfolio-validation/evaluations/'+f['id']);self.assertTrue(e['accepted'])
                self.assertEqual(len(api('/api/portfolio-validation/freezes')),1);self.assertEqual(len(api('/api/portfolio-validation/evaluations')),1)
                state=api('/api/portfolio-validation/change',dict(action='activate',expected_active=None,reason='合成HTTP流程',freeze_id=f['id']))
                self.assertEqual(state['active_id'],f['id']);self.assertEqual(api('/api/portfolio-validation/state'),state)
                with self.assertRaises(HTTPError) as exc:api('/api/portfolio-validation/freeze',dict(development_run_id='missing'))
                self.assertEqual(exc.exception.code,400)
            finally:
                if hasattr(server,'lab'):server.lab.close()
                server.shutdown();server.server_close();t.join()
