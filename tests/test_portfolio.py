import copy
import json
import tempfile
import threading
import unittest
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from app import Handler
from quantify import datasets
from quantify.jobs import QueueManager
from quantify.lab import Lab
from quantify.portfolio import Account, parameters, simulate
from quantify.portfolio_service import PortfolioService
from quantify.workbench import Workbench
from test_lab import wait_job


def order(side='buy',quantity=100,symbol='A',signal='2024-01-02'):
    return dict(side=side,quantity=quantity,symbol=symbol,signal_date=signal)


def state(**changes):
    return dict(suspended=0,limit_up=11,limit_down=9,no_price_limit=0,**changes)


class LedgerTests(unittest.TestCase):
    def account(self,**p):
        return Account(parameters(dict(initial_cash=10000,slippage_bps=0)|p))

    def test_exact_cash_cost_basis_tax_and_minimum_commission(self):
        a=self.account()
        a.fill(order(), '2024-01-03', {'open':10},state())
        self.assertEqual(a.cash,Decimal('8994.99'))
        self.assertEqual(a.lots['A'][0]['basis'],Decimal('1005.01'))
        a.fill(order('sell'), '2024-01-04', {'open':10.5},state())
        self.assertEqual(a.cash,Decimal('10039.45'))
        self.assertEqual(a.realized,Decimal('39.45'))
        self.assertEqual(a.fees,Decimal('10.55'))
        self.assertEqual(a.trades[-1]['stamp_tax'],.53)
        p=a.mark('2024-01-04',{'A':10.5})
        self.assertEqual(p['unrealized_pnl'],0)
        self.assertEqual(p['equity'],10039.45)

    def test_fifo_partial_sale_and_cent_reconciliation(self):
        a=self.account()
        a.fill(order(quantity=300),'2024-01-03',{'open':10},state())
        a.fill(order(quantity=100),'2024-01-04',{'open':10.01},state())
        basis=a.lots['A'][0]['basis'];a.fill(order('sell',100),'2024-01-05',{'open':10.2},state())
        self.assertEqual(a.quantity('A'),300)
        self.assertEqual(a.lots['A'][0]['basis'],basis-Decimal('1001.68'))
        p=a.mark('2024-01-05',{'A':10.2})
        self.assertAlmostEqual(p['equity'],10000+p['realized_pnl']+p['unrealized_pnl'],places=7)
        a.fill(order('sell',300),'2024-01-08',{'open':10.3},state())
        self.assertEqual(a.quantity('A'),0)
        self.assertEqual(a.mark('2024-01-08',{'A':10.3})['unrealized_pnl'],0)

    def test_t_plus_one_lot_and_no_short(self):
        a=self.account();a.fill(order(quantity=200),'2024-01-03',{'open':10},state())
        r=a.fill(order('sell',300),'2024-01-03',{'open':10},state())
        self.assertEqual(r['reason'],'t_plus_one_or_no_position');self.assertEqual(a.quantity('A'),200)
        r=a.fill(order('sell',300),'2024-01-04',{'open':10},state())
        self.assertEqual(r['status'],'partial');self.assertEqual(r['filled_quantity'],200)
        for q in (0,50,101,True):
            with self.assertRaises(ValueError):a.fill(order(quantity=q),'2024-01-05',{'open':10},state())

    def test_cash_constraint_partial_fill_never_borrows(self):
        a=self.account();r=a.fill(order(quantity=1000),'2024-01-03',{'open':10},state())
        self.assertEqual(r['filled_quantity'],900);self.assertEqual(r['status'],'partial')
        self.assertGreaterEqual(a.cash,0)
        r=a.fill(order(quantity=100),'2024-01-04',{'open':10.5},state())
        self.assertEqual(r['reason'],'insufficient_cash')

    def test_suspension_directional_limits_and_adverse_tick(self):
        a=self.account()
        self.assertEqual(a.fill(order(),'2024-01-03',{'open':10},{**state(),'suspended':1})['reason'],'suspended')
        self.assertEqual(a.fill(order(),'2024-01-03',{'open':11},state())['reason'],'limit_up')
        # Buying at the lower limit is permitted by this conservative open-price model.
        self.assertEqual(a.fill(order(),'2024-01-03',{'open':9},state())['status'],'filled')
        self.assertEqual(a.fill(order('sell'),'2024-01-04',{'open':9},state())['reason'],'limit_down')
        self.assertEqual(a.fill(order('sell'),'2024-01-04',{'open':11},state())['status'],'filled')
        a=self.account(slippage_bps=5)
        a.fill(order(),'2024-01-03',{'open':10},state())
        self.assertEqual(a.trades[-1]['price'],10.01)
        self.assertEqual(a.slippage,Decimal('1.00'))
        self.assertEqual(a.fill(order(),'2024-01-04',{'open':10.99},state())['reason'],'limit_up')

    def replay(self,opened=10,suspended=False):
        dates=[date(2024,1,31),date(2024,2,29)]
        panel=[{s:dict(close=10,quality=q) for s,q in [('A',2),('B',1),('C',0)]}]*2
        daily={d:{s:dict(open=o,close=10) for s in 'ABC'} for d,o in [('2024-01-31',10),('2024-02-01',opened),('2024-02-02',10)]}
        states={d:{s:dict(suspended=int(suspended and d=='2024-02-01'),no_price_limit=1) for s in 'ABC'} for d in daily}
        return simulate(dates=dates,panel=panel,daily=daily,states=states,factor=dict(id='quality',kind='quality'),config=parameters(dict(initial_cash=10000,top_k=1,cash_buffer=0,slippage_bps=0)),signal_dates={'2024-01-31'})

    def test_future_open_never_resizes_signal_order_and_no_terminal_liquidation(self):
        normal=self.replay();gap=self.replay(opened=20)
        self.assertEqual(normal['selected']['orders'][0]['quantity'],1000)
        self.assertEqual(gap['selected']['orders'][0]['quantity'],1000)
        self.assertEqual(gap['selected']['orders'][0]['filled_quantity'],400)
        self.assertEqual(gap['selected']['curve'][0]['nav'],1)
        self.assertEqual(gap['selected']['summary']['trade_count'],1)
        self.assertTrue(gap['selected']['holdings'])
        # Fee + negative mark-to-market, independently expected.
        self.assertEqual(gap['selected']['summary']['ending_equity'],5994.92)
        self.assertEqual(gap['selected']['summary']['max_drawdown'],-.400508)

    def test_failed_order_is_not_carried_or_retried(self):
        r=self.replay(suspended=True)
        self.assertEqual(len(r['selected']['orders']),1)
        self.assertEqual(r['selected']['orders'][0]['reason'],'suspended')
        self.assertEqual(r['selected']['summary']['ending_equity'],10000)
        self.assertFalse(r['selected']['trades'])

    def test_sells_before_buys_and_final_pending_is_not_filled(self):
        ds=[date(2024,1,31),date(2024,2,29),date(2024,3,29)]
        panel=[{s:dict(close=10,quality=q) for s,q in [('A',0),('B',2),('C',1)]},
               {s:dict(close=10,quality=q) for s,q in [('A',2),('B',0),('C',1)]},
               {s:dict(close=10,quality=q) for s,q in [('A',0),('B',0),('C',2)]}]
        stamps=['2024-01-31','2024-02-01','2024-02-29','2024-03-01','2024-03-29']
        bars={d:{s:dict(open=10,close=10) for s in 'ABC'} for d in stamps}
        states={d:{s:dict(suspended=0,no_price_limit=1) for s in 'ABC'} for d in stamps}
        r=simulate(dates=ds,panel=panel,daily=bars,states=states,factor=dict(id='q',kind='quality'),config=parameters(dict(top_k=1,cash_buffer=0)),signal_dates={d.isoformat() for d in ds})
        self.assertEqual([(t['symbol'],t['side']) for t in r['selected']['trades'] if t['date']=='2024-03-01'],[('B','sell'),('A','buy')])
        self.assertTrue(any(o['reason']=='no_next_bar' for o in r['selected']['orders']))
        self.assertFalse(any(t['date']=='2024-03-29' for t in r['selected']['trades']))


class PortfolioServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.wb=Workbench(cls.temp.name);cls.s=PortfolioService(cls.wb)
        cls.bundle=cls.s.demo();cls.spec=cls.bundle['spec'];cls.result=cls.s.run(spec_id=cls.spec['id'])

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def create(self,**changes):
        return self.s.create_spec(**(dict(experiment_id=self.spec['experiment_id'],factor_id=self.spec['factor_id'],daily_id=self.spec['daily_id'],execution_id=self.spec['execution_id'])|changes))

    def save(self,kind,rows):
        return datasets.import_snapshot(self.wb.store,kind=kind,raw_csv=datasets.csv_text(kind,rows),source='测试修订',price_basis='not_applicable' if kind=='execution' else 'unadjusted',is_demo=True)

    def test_replay_and_independently_reconstruct_every_daily_balance(self):
        self.assertEqual(self.result['status'],'completed',self.result.get('error'))
        repeated=self.s.run(spec_id=self.spec['id'])
        self.assertNotEqual(repeated['id'],self.result['id']);self.assertEqual(repeated['report'],self.result['report'])
        report=self.result['report'];daily={}
        for row in self.wb.store.get('snapshot',self.spec['daily_id'])['rows']:daily.setdefault(row['date'],{})[row['symbol']]=row['close']
        for key in ('selected','baseline'):
            cash=Decimal(100000);positions={};fees=Decimal(0)
            for p in report[key]['curve']:
                for t in [t for t in report[key]['trades'] if t['date']==p['date']]:
                    q=t['quantity'];self.assertEqual(q%100,0)
                    gross=Decimal(str(t['price']))*q;fee=sum(Decimal(str(t[k])) for k in ('commission','transfer_fee','stamp_tax'))
                    cash+=gross-fee if t['side']=='sell' else -gross-fee;fees+=fee
                    positions[t['symbol']]=positions.get(t['symbol'],0)+(q if t['side']=='buy' else -q)
                    self.assertEqual(cash,Decimal(str(t['cash_after'])))
                value=sum((Decimal(str(daily[p['date']][s]))*q for s,q in positions.items()),Decimal(0))
                self.assertEqual(Decimal(str(p['cash'])),cash);self.assertEqual(Decimal(str(p['equity'])),cash+value)
                self.assertAlmostEqual(p['equity'],100000+p['realized_pnl']+p['unrealized_pnl'],places=7)
                self.assertGreaterEqual(cash,0);self.assertEqual(Decimal(str(p['fees'])),fees)
        self.assertTrue(report['is_demo']);self.assertFalse(report['protocol']['independent_final_validation'])
        self.assertLess(report['protocol']['end'],report['protocol']['holdout_start'])

    def test_holdout_changes_do_not_reach_portfolio(self):
        cutoff=self.spec['holdout_start'];store=self.wb.store
        m=store.get('snapshot',self.spec['monthly_id']);d=store.get('snapshot',self.spec['daily_id']);e=store.get('snapshot',self.spec['execution_id'])
        for row in m['rows']:
            if row['date']>=cutoff:row['close']*=2
        for row in d['rows']:
            if row['date']>=cutoff:
                for k in ('open','high','low','close'):row[k]*=3
        for row in e['rows']:
            if row['date']>=cutoff:row.update(suspended=1)
        monthly=self.save('monthly_price',m['rows']);daily=self.save('daily',d['rows']);execution=self.save('execution',e['rows'])
        experiment=self.wb.run(snapshot_id=monthly['id'],factor_ids=[self.spec['factor_id']])
        spec=self.create(experiment_id=experiment['id'],daily_id=daily['id'],execution_id=execution['id'])
        self.assertEqual(self.s.run(spec_id=spec['id'])['report'],self.result['report'])

    def test_missing_states_failure_is_persisted(self):
        rows=self.wb.store.get('snapshot',self.spec['execution_id'])['rows']
        rows=[r for r in rows if r['date']!=self.spec['start']]
        snap=self.save('execution',rows);spec=self.create(execution_id=snap['id'])
        r=self.s.run(spec_id=spec['id'])
        self.assertEqual(r['status'],'failed');self.assertIn('完整覆盖',r['error'])
        self.assertEqual(self.wb.store.get('portfolio_run',r['id']),r)

    def test_reject_future_availability_zero_volume_limits_and_price_mismatch(self):
        original=self.wb.store.get('snapshot',self.spec['daily_id'])['rows']
        index=next(i for i,r in enumerate(original) if r['date']==self.spec['start'])
        for changes,message in [({'available_at':'2026-01-01'},'尚不可用'),({'volume':0},'成交量为零'),({'high':999999},'超出声明'),({'close':original[index]['close']+.01,'high':original[index]['high']+.01},'不一致')]:
            with self.subTest(changes=changes):
                rows=copy.deepcopy(original);rows[index].update(changes);snap=self.save('daily',rows)
                spec=self.create(daily_id=snap['id']);r=self.s.run(spec_id=spec['id'])
                self.assertEqual(r['status'],'failed');self.assertIn(message,r['error'])

    def test_spec_versions_parameters_and_source_binding(self):
        child=self.create(parent_id=self.spec['id'],config={'initial_cash':50000},name='第二版')
        self.assertNotEqual(child['id'],self.spec['id']);self.assertEqual(child['parent_id'],self.spec['id'])
        self.assertEqual(self.wb.store.get('portfolio_spec',self.spec['id'])['config']['initial_cash'],100000)
        for cfg in ({'rebalance_months':2},{'top_k':5},{'initial_cash':True},{'cash_buffer':1},{'secret':1}):
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):self.create(config=cfg)
        wrong=next(f for f in self.wb.store.list('factor') if f['kind']=='quality')
        with self.assertRaises(ValueError):self.create(factor_id=wrong['id'])
        with patch('quantify.portfolio_service.engine_fingerprint',return_value={'changed':True}):
            with self.assertRaises(ValueError):self.create()
            self.assertEqual(self.s.run(spec_id=self.spec['id'])['status'],'failed')

    def test_execution_csv_atomic_validation_and_revision(self):
        rows=[dict(date='2024-01-02',symbol='A',suspended=0,limit_up=11,limit_down=9,no_price_limit=0,available_at='2024-01-02')]
        first=self.save('execution',rows);before=len(self.wb.store.list('snapshot'))
        for change in ({'limit_up':''},{'suspended':2},{'available_at':'2024-01-03'},{'no_price_limit':1},{'limit_down':12}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.save('execution',[rows[0]|change])
        self.assertEqual(len(self.wb.store.list('snapshot')),before)
        second=datasets.import_snapshot(self.wb.store,kind='execution',raw_csv=datasets.csv_text('execution',[rows[0]|{'suspended':1}]),source='测试修订',price_basis='not_applicable',is_demo=True,parent_id=first['id'])
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(self.wb.store.get('snapshot',first['id'])['rows'][0]['suspended'],0)
        unrestricted=self.save('execution',[rows[0]|dict(no_price_limit=1,limit_up='',limit_down='')])
        self.assertIsNone(unrestricted['rows'][0]['limit_up'])

    def test_actual_worker_replay_failure_retry_and_restart(self):
        lab=Lab(self.temp.name)
        try:
            job=lab.portfolio.submit(spec_id=self.spec['id']);done=wait_job(lab.store,job['id'])
            self.assertEqual(done['status'],'succeeded',done.get('error'))
            r=lab.workbench.store.get('portfolio_run',done['result']['portfolio_run_id'])
            self.assertEqual(r['report'],self.result['report'])
            replay=lab.portfolio.submit(replay_id=r['id']);self.assertEqual(wait_job(lab.store,replay['id'])['status'],'succeeded')
            states=self.wb.store.get('snapshot',self.spec['execution_id'])['rows'];states=[r for r in states if r['date']!=self.spec['start']]
            bad=self.create(execution_id=self.save('execution',states)['id']);job=lab.portfolio.submit(spec_id=bad['id'])
            failed=wait_job(lab.store,job['id']);self.assertEqual(failed['status'],'failed');self.assertIn('portfolio_run_id',failed['result'])
            retried=lab.retry(job['id']);self.assertEqual(retried['retry_of'],job['id']);self.assertEqual(wait_job(lab.store,retried['id'])['status'],'failed')
        finally:lab.close()
        reopened=Lab(self.temp.name)
        try:self.assertEqual(reopened.store.job(done['id'])['status'],'succeeded')
        finally:reopened.close()


class PortfolioHTTPTests(unittest.TestCase):
    def test_http_assets_demo_and_queued_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def api(path,payload=None):
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=20) as r:
                    return r.read() if path in ('/portfolio','/portfolio.js') or 'templates/' in path else json.load(r)
            try:
                for path in ('/portfolio','/portfolio.js','/api/workspace/templates/execution.csv'):self.assertTrue(api(path))
                bundle=api('/api/portfolio/demo',{});job=api('/api/portfolio/runs',{'spec_id':bundle['spec']['id']})
                done=wait_job(server.lab.store,job['id']);self.assertEqual(done['status'],'succeeded',done.get('error'))
                r=api('/api/portfolio/runs/'+done['result']['portfolio_run_id']);self.assertEqual(r['status'],'completed')
                self.assertEqual(len(api('/api/portfolio/runs')),1);self.assertEqual(len(api('/api/portfolio/specs')),1)
                with self.assertRaises(HTTPError) as e:api('/api/portfolio/runs',{})
                self.assertEqual(e.exception.code,400);e.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join()
                if hasattr(server,'lab'):server.lab.close()


if __name__=='__main__':unittest.main()
