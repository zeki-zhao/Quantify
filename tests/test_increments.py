import copy
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Handler
from quantify import datasets, factors
from quantify.increment_service import IncrementService
from quantify.increments import analyze, compact_report
from quantify.lab import Lab, initial_progress
from quantify.lifecycle import Lifecycle
from quantify.portfolio_service import PortfolioService
from quantify.expressions import validate_expression
from quantify.trial_audit import audit_trials
from quantify.model_api import compact_context
from quantify.research_tools import ResearchTools
from quantify.trial_audit import factor_signature
from quantify.workbench import Workbench
from quantify.worker import execute
from test_lab import proposal, wait_job
from test_model_api import KEYS, STOP, Transport, response


class BlendMathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.w = Workbench(self.temp.name); self.store = self.w.store
        self.q = next(f for f in self.store.list('factor') if f['kind'] == 'quality')
        self.v = next(f for f in self.store.list('factor') if f['kind'] == 'value')
        self.components = [dict(factor_id=self.q['id'], weight=3), dict(factor_id=self.v['id'], weight=1)]
        self.blend = factors.create_blend(self.store, components=self.components, name='配对', reason='算例')

    def panel(self):
        return [{s: dict(close=10., quality=q, value=v) for s, q, v in [('A', 1, 3), ('B', 1, 2), ('C', 3, 1)]},
                {s: dict(close=10., quality=-999, value=999) for s in 'ABC'},
                {s: dict(close=p, quality=888, value=-888) for s, p in zip('ABC', [5, 10, 20])}]

    def test_weighted_average_ranks_ties_and_no_future_inputs(self):
        panel = self.panel(); scores = factors.calculate(panel, 0, [self.blend])
        # quality percentiles .25, .25, 1; value percentiles 1, .5, 0.
        self.assertEqual([scores[s][self.blend['id']] for s in 'ABC'], [.4375, .3125, .75])
        panel[1]['A']['quality'] = 1e8
        self.assertEqual(factors.calculate(panel, 0, [self.blend]), scores)
        self.assertEqual(factors.required_fields([self.blend]), {'quality', 'value'})
        self.assertEqual(factors.history_length([self.blend]), 0)

    def test_immutable_refs_reordered_inputs_and_structural_signature(self):
        reordered = factors.create_blend(self.store, components=self.components[::-1], name='配对', reason='算例')
        self.assertEqual(reordered['id'], self.blend['id'])
        scaled = factors.create_blend(self.store, components=[dict(factor_id=c['factor_id'], weight=c['weight']*2) for c in self.components], name='比例相同', reason='对照')
        self.assertEqual(factor_signature(scaled), factor_signature(self.blend))
        changed = factors.create_blend(self.store, components=[dict(factor_id=self.q['id'],weight=1), dict(factor_id=self.v['id'],weight=3)], name='反向权重', reason='对照',parent_id=self.blend['id'])
        self.assertNotEqual(factor_signature(changed), factor_signature(self.blend))
        self.assertEqual(self.store.get('factor', self.blend['id']), self.blend)
        for components in ([self.components[0]]*2, [dict(factor_id=self.q['id'],weight=0),self.components[1]],
                           [dict(factor_id=self.q['id'],weight=True),self.components[1]],
                           [dict(factor_id=self.blend['id'],weight=1),self.components[1]]):
            with self.assertRaises(ValueError): factors.create_blend(self.store,components=components,name='非法',reason='测试')
        renamed = factors.create_version(self.store,parent_id=self.blend['id'],name='重命名',reason='测试')
        self.assertEqual(renamed['components'],self.blend['components'])

    def test_known_paired_return_cost_and_drop_one(self):
        aug = factors.augment(self.store, self.q, self.v, .75)
        report = analyze(dates=[date(2024,1,1),date(2024,2,1),date(2024,3,1)], panel=self.panel(),
                         base=self.q,candidate=self.v,augmented=aug,windows=[(0,1)],horizon=1,top_k=1,cost_bps=100,objective=None)
        s = report['summary']; self.assertEqual(s['metrics']['base']['mean_return'], .99)
        self.assertEqual(s['metrics']['candidate']['mean_return'], -.51)
        self.assertEqual(s['metrics']['augmented']['mean_return'], -.51)
        self.assertEqual(s['augmented_minus_base']['mean_return'], -1.5)
        self.assertFalse(s['decision']['accepted'])
        removed = next(a for a in s['ablations'] if a['omitted_factor_id']==self.v['id'])
        self.assertEqual(removed['augmented_minus_without'],s['augmented_minus_base'])
        # Every variant must use the same mature dates, costs and number of stocks.
        for v in report['windows'][0]['variants'].values(): self.assertEqual(v['metrics']['observations'],1)

    def test_missing_rejects_entire_paired_window_and_ties_use_code(self):
        panel = self.panel(); panel[0]['B']['value'] = None
        r = analyze(dates=[date(2024,1,1),date(2024,2,1),date(2024,3,1)],panel=panel,base=self.q,
                    candidate=self.v,augmented=self.blend,windows=[(0,1)],horizon=1,top_k=1,cost_bps=0,objective=None)
        self.assertEqual(r['summary']['paired_windows'],0);self.assertFalse(r['summary']['comparison_eligible'])
        self.assertFalse(r['summary']['decision']['accepted']);self.assertIsNone(r['summary']['augmented_minus_base'])
        self.assertNotIn('variants',r['windows'][0])
        panel=self.panel()
        for row in panel[0].values():row['quality']=1;row['value']=1
        r=analyze(dates=[date(2024,1,1),date(2024,2,1),date(2024,3,1)],panel=panel,base=self.q,candidate=self.v,
                  augmented=self.blend,windows=[(0,1)],horizon=1,top_k=1,cost_bps=0,objective=None)
        self.assertTrue(all(v['symbols']==['A'] for v in r['windows'][0]['variants'].values()))

    def test_extreme_valid_weights_preserve_ratios_and_drop_one_ties(self):
        tiny=factors.create_blend(self.store,components=[dict(factor_id=self.q['id'],weight=.000001),
                                 dict(factor_id=self.v['id'],weight=1000000)],name='比例边界',reason='精确有理权重')
        candidate=factors.create_version(self.store,parent_id=self.q['id'],name='候选',reason='测试',expression='1')
        augmented=factors.augment(self.store,tiny,candidate,.99)
        factors.validate_spec(augmented)
        # Removing the added candidate preserves the original weights exactly, even at extreme ratios.
        remaining=[c for c in augmented['components'] if c['factor']['id']!=candidate['id']]
        removed=dict(id='removed',kind='blend',components=remaining)
        self.assertEqual([v[tiny['id']] for v in factors.calculate(self.panel(),0,[tiny]).values()],
                         [v['removed'] for v in factors.calculate(self.panel(),0,[removed]).values()])
        constant=factors.create_blend(self.store,components=[dict(factor_id=self.q['id'],weight=.1),dict(factor_id=self.v['id'],weight=.1)],name='同分',reason='算术对照')
        panel=self.panel()
        for s,q,v in [('A',1,3),('B',2,2),('C',3,1)]:panel[0][s].update(quality=q,value=v)
        self.assertEqual(set(v[constant['id']] for v in factors.calculate(panel,0,[constant]).values()),{.5})


class IncrementFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.lab=Lab(self.temp.name);self.addCleanup(self.lab.close)
        self.w=self.lab.workbench;self.store=self.lab.store
        self.snapshot=self.w.post('/api/workspace/demo',{})
        self.fs={f['kind']:f for f in self.store.list('factor')}
        self.base=factors.create_blend(self.store,components=[dict(factor_id=self.fs[k]['id'],weight=1) for k in ('momentum','quality')],name='原组合',reason='测试')
        self.e=self.w.run(snapshot_id=self.snapshot['id'],factor_ids=[self.base['id']])
        self.args=dict(experiment_id=self.e['id'],base_factor_id=self.base['id'],candidate_factor_id=self.fs['value']['id'])
        self.service=self.lab.increments

    def test_fixed_rule_matches_experiment_cache_and_holdout_invariance(self):
        r=self.service.run(**self.args);self.assertEqual(r['status'],'completed',r.get('error'))
        self.assertEqual(r,self.service.run(**self.args));s=r['report']['summary']
        self.assertEqual(s['metrics']['base'],self.e['report']['selected'])
        aug=self.w.run(**(self.e['request']|dict(factor_ids=[r['report']['augmented_factor']['id']])))
        self.assertEqual(s['metrics']['augmented'],aug['report']['selected'])
        self.assertEqual(s['expected_windows'],self.e['report']['selected']['observations']//2)
        self.assertLess(r['report']['protocol']['last_exit'],self.e['protocol']['holdout_start'])
        snapshot=self.store.get('snapshot',self.snapshot['id'])
        for row in snapshot['rows']:
            if row['date']>=self.e['protocol']['holdout_start']:row.update(total_return_index=123456789,quality=-999,value=999)
        changed=datasets.import_snapshot(self.store,kind='monthly',raw_csv=datasets.csv_text('monthly',snapshot['rows']),source='隔离哨兵',price_basis='total_return_index',is_demo=True)
        e=self.w.run(**(self.e['request']|dict(snapshot_id=changed['id'])))
        other=self.service.run(**(self.args|dict(experiment_id=e['id'])))
        for key in ('summary','windows','by_year','by_fold'):self.assertEqual(r['report'][key],other['report'][key])
        frozen=Lifecycle(self.w).freeze(experiment_id=aug['id'])
        refs=self.service.references(self.fs['quality']['id'])
        self.assertIn(self.base['id'],refs['blends']);self.assertIn(self.e['id'],refs['experiments'])
        self.assertIn(frozen['id'],refs['freezes']);self.assertIn(r['id'],refs['increments'])

    def test_queued_replay_validation_cancellation_retry_and_engine(self):
        job=self.service.submit(**self.args);done=wait_job(self.store,job['id']);self.assertEqual(done['status'],'succeeded',done)
        repeated=self.service.submit(replay_id=done['result']['increment_id']);again=wait_job(self.store,repeated['id'])
        self.assertEqual(done['result'],again['result']);self.assertTrue(again['progress']['cached'])
        counts=audit_trials(self.store,self.e['id'])['counts']
        self.assertEqual(counts['increment_reports'],1);self.assertEqual(counts['increment_jobs'],2)
        with self.assertRaisesRegex(ValueError,'暂停'):self.store.control(self.service.submit(**self.args)['id'],'pause')
        for changes in (dict(candidate_factor_id=self.fs['quality']['id']),dict(base_factor_id=self.fs['quality']['id']),dict(candidate_weight=1),dict(candidate_factor_id=self.base['id'])):
            with self.assertRaises(ValueError):self.service.request(**(self.args|changes))
        self.lab.close()
        queued=self.service.submit(**self.args);self.store.control(queued['id'],'cancel')
        retry=self.lab.retry(queued['id']);self.assertEqual(retry['retry_of'],queued['id'])
        # A stopped app's next startup preserves interruption instead of silently reissuing work.
        restarted=Lab(self.temp.name);self.addCleanup(restarted.close)
        self.assertEqual(self.store.job(retry['id'])['status'],'interrupted')
        with patch('quantify.increment_service.engine_fingerprint',return_value={}):
            with self.assertRaisesRegex(ValueError,'引擎'):self.service.submit(replay_id=done['result']['increment_id'])

    def test_manual_augment_scope_feedback_and_original_baseline_remains_fixed(self):
        job=self.lab.create_session(baseline_id=self.e['id'],goal='增量',research_mode='augment',candidate_weight=.3,max_rounds=2)
        self.store.append_proposals(job['id'],[proposal('value'),proposal('-volatility(6,0)')])
        done=wait_job(self.store,job['id']);self.assertEqual(done['status'],'succeeded',done)
        self.assertEqual(len(done['progress']['outcomes']),2)
        for o in done['progress']['outcomes']:
            self.assertIn(o['status'],('improved','not_improved'));self.assertEqual(o['increment']['status'],'completed')
            factor=self.store.get('factor',o['factor_id']);self.assertEqual(factor['parent_id'],self.base['id'])
            self.assertEqual(len(factor['components']),3)
            self.assertEqual(o['selected'],o['increment']['summary']['metrics']['augmented'])
        context=self.lab.research_package(job['id']);encoded=json.dumps(context)
        for forbidden in ('raw_csv','raw_import','DEMO1','123456789'):self.assertNotIn(forbidden,encoded)
        tools=ResearchTools(self.temp.name,self.e['id'],done['progress'],session_id=job['id'],research_mode='augment',candidate_weight=.3)
        with self.assertRaises(ValueError):tools.call('get_factor_increment',experiment_id='foreign')
        foreign=self.w.run(**self.e['request'])
        with self.assertRaises(ValueError):tools.call('get_factor_increment',experiment_id=foreign['id'])
        multi=self.w.run(**(self.e['request']|dict(factor_ids=[self.fs['quality']['id'],self.fs['value']['id']])))
        with self.assertRaisesRegex(ValueError,'仅绑定一个'):self.lab.create_session(baseline_id=multi['id'],goal='非法',research_mode='augment')

    def test_both_model_feedback_protocols_and_extra_fields_are_filtered(self):
        self.lab.close()
        with patch.dict('os.environ',KEYS):
            for provider in ('openai','deepseek'):
                self.lab.models.configure(provider=provider,model='fixture',input_price_per_million=.1,output_price_per_million=.1,currency='USD')
                j=self.lab.create_session(baseline_id=self.e['id'],goal='合成增量',research_mode='augment',provider=provider,max_rounds=2)
                transport=Transport([response(provider,proposal('value')),response(provider,STOP)])
                owner='fixture-'+provider;self.assertEqual(self.store.claim(owner)['id'],j['id'])
                with patch('quantify.model_calls.ModelHTTP',return_value=transport):execute(self.temp.name,j['id'],owner)
                done=self.store.job(j['id']);self.assertEqual(done['status'],'succeeded',done.get('error'))
                body=transport.calls[1][1];package=json.loads(body['input'][0]['content'] if provider=='openai' else body['messages'][-1]['content'])
                self.assertEqual(package['research_mode']['mode'],'augment')
                self.assertEqual(package['feedback'][0]['increment']['status'],'completed')
                self.assertEqual(package['factors'][0]['kind'],'blend');self.assertEqual(len(package['factors'][0]['components']),2)
                for forbidden in ('raw_csv','DEMO1','returns','symbols"'):self.assertNotIn(forbidden,json.dumps(package))
                context=self.lab.research_package(j['id'])['context']
                feedback=copy.deepcopy(done['progress']['outcomes']);feedback[0]['increment']['windows']=[dict(raw_csv='SENTINEL')]
                feedback[0]['increment']['summary']['metrics']['base']['returns']=['SENTINEL']
                self.assertNotIn('SENTINEL',json.dumps(compact_context(context,feedback,'test')))

    def test_augmented_pending_resume_reuses_factor_and_experiment(self):
        j=self.lab.create_session(baseline_id=self.e['id'],goal='恢复',research_mode='augment',max_rounds=1)
        self.lab.close();self.store.append_proposals(j['id'],[proposal('value')]);claimed=self.store.claim('fixture')
        progress=claimed['progress'];tools=ResearchTools(self.temp.name,self.e['id'],progress,session_id=j['id'],research_mode='augment')
        candidate=tools.call('create_factor_candidate',name='测试因子',expression='value',rationale='合成流程验证')
        eid=j['id']+'-round-1';tools.call('run_experiment',factor_id=candidate['id'],experiment_id=eid)
        progress.update(cursor=1,rounds=1,candidates=1,pending=dict(proposal=proposal('value'),round=1,stage='validated',factor_id=candidate['id'],normalized=validate_expression('value')['normalized'],experiment_id=eid))
        self.store.checkpoint(j['id'],'fixture',progress);self.store.finish(j['id'],'fixture','paused')
        self.store.control(j['id'],'resume');self.store.claim('resumed');execute(self.temp.name,j['id'],'resumed')
        done=self.store.job(j['id']);self.assertEqual(done['status'],'succeeded',done.get('error'))
        self.assertEqual(len(done['progress']['outcomes']),1);self.assertEqual(len(self.store.list('experiment')),2)
        self.assertEqual(done['progress']['outcomes'][0]['increment']['status'],'completed')

    def test_price_blend_portfolio_binding_and_reference(self):
        p=PortfolioService(self.w);fixture=p.demo()
        blend=factors.create_blend(self.store,components=[dict(factor_id=self.fs[k]['id'],weight=1) for k in ('momentum','low_volatility')],name='价格组合',reason='无财务依赖')
        e=self.w.run(snapshot_id=fixture['monthly_id'],factor_ids=[blend['id']]);self.assertEqual(e['status'],'completed')
        candidate=factors.create_version(self.store,parent_id=self.fs['momentum']['id'],name='12月',reason='价格',expression='momentum(12,0)')
        inc=self.service.run(experiment_id=e['id'],base_factor_id=blend['id'],candidate_factor_id=candidate['id'])
        self.assertEqual(inc['status'],'completed');self.assertEqual(inc['report']['return_basis'],'price')
        spec=p.create_spec(experiment_id=e['id'],factor_id=blend['id'],daily_id=fixture['daily_id'],execution_id=fixture['execution_id'])
        run=p.run(spec_id=spec['id']);self.assertEqual(run['status'],'completed',run.get('error'))
        self.assertIn(spec['id'],self.service.references(self.fs['low_volatility']['id'])['portfolio_specs'])


class IncrementHTTPTests(unittest.TestCase):
    def test_create_submit_export_and_references(self):
        with tempfile.TemporaryDirectory() as temp:
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def api(path,payload=None):
                with urlopen(Request('http://127.0.0.1:'+str(server.server_port)+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=20) as r:
                    return json.load(r) if r.headers.get_content_type()=='application/json' else r.read()
            try:
                self.assertTrue(api('/increments'));self.assertTrue(api('/increments.js'))
                snap=api('/api/workspace/demo',{});fs={f['kind']:f for f in api('/api/workspace/factors')}
                blend=api('/api/increments/blends',dict(components=[dict(factor_id=fs[k]['id'],weight=1) for k in ('momentum','quality')],name='HTTP',reason='合成'))
                e=api('/api/workspace/experiments',dict(snapshot_id=snap['id'],factor_ids=[blend['id']]))
                job=api('/api/increments/reports',dict(experiment_id=e['id'],base_factor_id=blend['id'],candidate_factor_id=fs['value']['id']))
                done=wait_job(server.lab.store,job['id']);self.assertEqual(done['status'],'succeeded',done)
                r=api('/api/increments/reports/'+done['result']['increment_id']);self.assertEqual(r['status'],'completed')
                self.assertFalse(r['report']['protocol']['holdout_evaluated']);self.assertEqual(len(api('/api/increments/reports')),1)
                self.assertIn(blend['id'],api('/api/increments/references/'+fs['quality']['id'])['blends'])
                with self.assertRaises(HTTPError) as err:api('/api/increments/blends',dict(components=[],name='invalid',reason='test'))
                self.assertEqual(err.exception.code,400)
            finally:
                if hasattr(server,'lab'):server.lab.close()
                server.shutdown();server.server_close();thread.join()
