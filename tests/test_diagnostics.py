import copy
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from app import Handler
from quantify import datasets,factors
from quantify.diagnostics import analyze,ranks,spearman
from quantify.diagnostics_service import DiagnosticService
from quantify.jobs import JobStore
from quantify.lab import Lab,initial_progress
from quantify.model_api import compact_context
from quantify.research import demo_csv
from quantify.research_tools import ResearchTools
from quantify.trial_audit import audit_trials,factor_signature
from quantify.workbench import Workbench
from test_lab import wait_job


def fixture(quality=None):
    dates=[date(2024,1,31),date(2024,2,29),date(2024,3,29)]
    panel=[]
    for m in range(3):
        panel.append({s:dict(close=(10*(i+1) if m==2 else 10),quality=(quality[i] if quality else i+1),value=3-i) for i,s in enumerate('ABC')})
    return dates,panel


def diagnostic(specs=None,quality=None,group_count=3):
    ds,p=fixture(quality)
    return analyze(dates=ds,panel=p,specs=specs or [dict(id='q',kind='quality')],windows=[(0,1)],horizon=1,cost_bps=100,group_count=group_count)


class DiagnosticMathTests(unittest.TestCase):
    def test_average_ranks_and_known_spearman(self):
        self.assertEqual(ranks([4,2,2,1]),[4,2.5,2.5,1])
        self.assertEqual(spearman([1,2,3],[3,6,9]),1)
        self.assertEqual(spearman([1,2,3],[9,6,3]),-1)
        self.assertAlmostEqual(spearman([1,1,3],[1,2,3]),.8660254037844386)
        self.assertIsNone(spearman([1,1,1],[1,2,3]))
        self.assertIsNone(spearman([1,2],[1,2]))
        with self.assertRaises(ValueError):spearman([1],[1,2])

    def test_delayed_entry_cost_and_group_spread(self):
        r=diagnostic();f=r['factors'][0];w=f['windows'][0]
        self.assertEqual((w['signal'],w['entry'],w['exit']),('2024-01-31','2024-02-29','2024-03-29'))
        self.assertEqual(w['rank_ic'],1)
        for g,expected in zip(w['groups'],[-.01,.99,1.99]):self.assertAlmostEqual(g['mean_return'],expected)
        self.assertAlmostEqual(w['high_minus_low'],2)
        self.assertEqual(f['summary']['computed_scores'],3)
        self.assertEqual(f['summary']['score_coverage'],1)
        self.assertIsNone(f['summary']['rank_ic_std'])

    def test_tied_scores_are_not_split_and_constant_is_undefined(self):
        r=diagnostic(quality=[1,1,1]);f=r['factors'][0];w=f['windows'][0]
        self.assertIsNone(w['rank_ic']);self.assertIsNone(w['high_minus_low'])
        self.assertEqual([g['observations'] for g in w['groups']],[0,3,0])
        self.assertEqual(f['summary']['constant_or_undefined_ic'],1)
        self.assertEqual(f['summary']['ic_windows'],0)
        r=diagnostic(quality=[1,1,3]);w=r['factors'][0]['windows'][0]
        self.assertEqual([g['observations'] for g in w['groups']],[0,2,1])
        self.assertIsNone(w['high_minus_low'])

    def test_missing_factor_inputs_fail_cross_section_without_zero_fill(self):
        r=diagnostic(specs=[dict(id='q',kind='quality'),dict(id='v',kind='value')],quality=[1,None,3])
        q,v=r['factors'];self.assertEqual(q['summary']['score_coverage'],0)
        self.assertAlmostEqual(q['summary']['input_coverage'],2/3)
        self.assertEqual(q['windows'][0]['status'],'unavailable');self.assertIn('缺少',q['windows'][0]['reason'])
        self.assertEqual(v['summary']['score_coverage'],1)
        pair=next(c for c in r['correlations'] if c['left_id']=='q' and c['right_id']=='v')
        self.assertEqual(pair['common_scored_windows'],0);self.assertIsNone(pair['mean_rank_correlation'])
        self.assertEqual(r['common_scored_windows'],0)

    def test_factor_correlation_uses_paired_windows_and_no_future_score(self):
        specs=[dict(id='q',kind='quality'),dict(id='v',kind='value')]
        ds,p=fixture();original=factors.calculate(p,0,specs)
        for month in p[1:]:
            for row in month.values():row['quality']=999;row['value']=-999
        self.assertEqual(factors.calculate(p,0,specs),original)
        r=diagnostic(specs=specs)
        pair=next(c for c in r['correlations'] if c['left_id']=='q' and c['right_id']=='v')
        self.assertEqual(pair['mean_rank_correlation'],-1);self.assertEqual(pair['valid_windows'],1)
        self.assertEqual(r['factors'][0]['by_year'][0]['year'],'2024')
        self.assertEqual(r['factors'][0]['by_fold'][0]['fold'],1)


class DiagnosticServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.wb=Workbench(self.temp.name);self.store=self.wb.store;self.s=DiagnosticService(self.wb)
        self.snap=datasets.import_snapshot(self.store,kind='monthly',raw_csv=demo_csv(),source='合成诊断',price_basis='total_return_index',is_demo=True)
        self.parent=next(f for f in self.store.list('factor') if f['kind']=='momentum')
        self.e=self.wb.run(snapshot_id=self.snap['id'],factor_ids=[self.parent['id']])

    def test_cache_known_source_windows_and_holdout_invariance(self):
        first=self.s.run(experiment_id=self.e['id']);self.assertEqual(first['status'],'completed',first.get('error'))
        self.assertEqual(first,self.s.run(experiment_id=self.e['id']))
        report=first['report'];self.assertFalse(report['protocol']['holdout_evaluated'])
        self.assertLess(report['protocol']['last_exit'],self.e['protocol']['holdout_start'])
        expected=self.e['report']['selected']['observations']//self.e['request']['top_k']
        self.assertEqual(report['expected_windows'],expected)
        changed=copy.deepcopy(self.snap['rows'])
        for row in changed:
            if row['date']>=self.e['protocol']['holdout_start']:row.update(total_return_index=99999999,quality=123456,value=-333)
        snap=datasets.import_snapshot(self.store,kind='monthly',raw_csv=datasets.csv_text('monthly',changed),source='合成诊断',price_basis='total_return_index',is_demo=True)
        second=self.wb.run(**(self.e['request']|dict(snapshot_id=snap['id'])))
        other=self.s.run(experiment_id=second['id'])['report']
        for k in ('factors','correlations','common_scored_windows','expected_windows'):self.assertEqual(report[k],other[k])
        for forbidden in ('raw_csv','raw_import','DEMO1'):self.assertNotIn(forbidden,json.dumps(first))

    def test_additional_formula_failure_coverage_and_validation(self):
        constant=factors.create_version(self.store,parent_id=self.parent['id'],name='常数',reason='验证并列',expression='1')
        broken=factors.create_version(self.store,parent_id=self.parent['id'],name='除零',reason='验证拒算',expression='1 / (quality-quality)')
        r=self.s.run(experiment_id=self.e['id'],factor_ids=[constant['id'],broken['id']])
        self.assertEqual(r['status'],'completed')
        a,b=r['report']['factors'];self.assertIsNone(a['summary']['mean_rank_ic'])
        self.assertEqual(b['summary']['score_coverage'],0);self.assertEqual(b['summary']['input_coverage'],1)
        self.assertTrue(all(w['status']=='unavailable' for w in b['windows']))
        for changes in ({'factor_ids':[]},{'factor_ids':[self.parent['id']]*2},{'group_count':6},{'group_count':True},{'factor_ids':'bad'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.s.request(**(dict(experiment_id=self.e['id'])|changes))
        with patch('quantify.diagnostics_service.engine_fingerprint',return_value={}):
            with self.assertRaises(ValueError):self.s.request(experiment_id=self.e['id'])

    def test_ai_tool_scope_and_explicit_model_summary_whitelist(self):
        progress=initial_progress(self.e);tools=ResearchTools(self.temp.name,self.e['id'],progress)
        context=tools.call('get_research_context');summary=context['diagnostics']
        self.assertEqual(summary['report']['status'],'completed');self.assertIn('trial_counts',summary)
        foreign=self.wb.run(**self.e['request'])
        with self.assertRaises(ValueError):tools.call('get_factor_diagnostics',experiment_id=foreign['id'])
        other=next(f for f in self.store.list('factor') if f['kind']=='quality')
        with self.assertRaises(ValueError):tools.call('get_factor_diagnostics',experiment_id=self.e['id'],factor_ids=[other['id']])
        summary['report']['raw_csv']='PRIVATE_ROWS';summary['report']['factors'][0]['summary']['future_return']='FINAL_SECRET'
        summary['report']['correlations'][0]['windows']='PRIVATE_WINDOWS';summary['trial_counts']['foreign_results']='PRIVATE_RESULTS'
        encoded=json.dumps(compact_context(context,[],'检验'))
        for forbidden in ('PRIVATE_ROWS','FINAL_SECRET','PRIVATE_WINDOWS','PRIVATE_RESULTS','raw_csv'):self.assertNotIn(forbidden,encoded)
        self.assertIn('mean_rank_ic',encoded)

    def test_actual_diagnostic_worker_replay_and_restart(self):
        lab=Lab(self.temp.name)
        try:
            j=lab.diagnostics.submit(experiment_id=self.e['id']);done=wait_job(lab.store,j['id'])
            self.assertEqual(done['status'],'succeeded',done.get('error'));artifact_id=done['result']['diagnostic_id']
            replay=lab.diagnostics.submit(replay_id=artifact_id);replayed=wait_job(lab.store,replay['id'])
            self.assertEqual(replayed['status'],'succeeded');self.assertEqual(replayed['result']['diagnostic_id'],artifact_id)
            self.assertTrue(replayed['progress']['cached'])
        finally:lab.close()
        again=Lab(self.temp.name)
        try:self.assertEqual(again.store.job(j['id'])['result']['diagnostic_id'],artifact_id)
        finally:again.close()

    def test_trial_census_cross_sessions_pending_invalid_retry_and_replays(self):
        store=JobStore(self.temp.name)
        self.wb.run(**self.e['request'])
        self.wb.run(**(self.e['request']|dict(top_k=100)))
        proposals=[dict(action='propose',name='甲',expression='momentum(6,0)',rationale='合成'),dict(action='propose',name='乙',expression='momentum( 6, 0 )',rationale='合成')]
        for i,p in enumerate(proposals):
            progress=dict(cursor=1,outcomes=[dict(round=1,proposal=p,status='not_improved')],pending=None)
            j=store.create('research',dict(baseline_id=self.e['id'],proposals=[p]),10,progress,status='waiting_input')
            store.event(j['id'],'proposal',dict(round=1,proposal=p))
        invalid=dict(action='propose',name='坏公式',expression='__import__("os")',rationale='验证非法')
        pending=store.create('research',dict(baseline_id=self.e['id'],proposals=[invalid,proposals[0]]),10,dict(cursor=1,outcomes=[],pending=dict(round=1,proposal=invalid)),status='waiting_input')
        store.control(pending['id'],'cancel')
        overall=audit_trials(store);c=overall['counts']
        self.assertEqual(c['experiments'],3);self.assertEqual(c['experiment_statuses'],{'completed':2,'failed':1})
        self.assertEqual(c['repeated_experiments'],1);self.assertEqual(c['sessions'],3)
        self.assertEqual(c['proposals_consumed'],3);self.assertEqual(c['unique_valid_formulas'],1)
        self.assertEqual(c['repeated_proposals'],1);self.assertEqual(c['invalid_formulas'],1)
        self.assertEqual(c['proposal_statuses']['unresolved_cancelled'],1);self.assertEqual(c['submitted_unconsumed'],1)
        group=overall['repeated_formula_groups'][0];self.assertEqual(group['attempts'],2);self.assertEqual(group['sessions'],2)
        scoped=audit_trials(store,self.e['id'])
        self.assertEqual(scoped['counts']['experiments'],2);self.assertEqual(scoped['scope'],'same_protocol')
        before=copy.deepcopy(c);self.s.run(experiment_id=self.e['id'])
        self.assertEqual(audit_trials(store)['counts'],before)
        expression=factors.create_version(store,parent_id=self.parent['id'],name='同定义',reason='重复识别',expression='momentum(6,0)')
        self.assertEqual(factor_signature(expression),factor_signature(self.parent))
        self.assertTrue(audit_trials(store)['repeated_factor_definitions'])


class DiagnosticHTTPTests(unittest.TestCase):
    def test_http_submission_reports_trials_and_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.data_dir=temp
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def api(path,payload=None):
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'}),timeout=20) as r:
                    return r.read() if path in ('/diagnostics','/diagnostics.js') else json.load(r)
            try:
                self.assertTrue(api('/diagnostics'));self.assertTrue(api('/diagnostics.js'))
                snap=api('/api/workspace/demo',{});fs=api('/api/workspace/factors')
                e=api('/api/workspace/experiments',dict(snapshot_id=snap['id'],factor_ids=[fs[0]['id']]))
                job=api('/api/diagnostics/reports',dict(experiment_id=e['id']));done=wait_job(server.lab.store,job['id'])
                self.assertEqual(done['status'],'succeeded',done.get('error'))
                report=api('/api/diagnostics/reports/'+done['result']['diagnostic_id']);self.assertEqual(report['status'],'completed')
                self.assertEqual(len(api('/api/diagnostics/reports')),1)
                self.assertEqual(api('/api/diagnostics/trials')['counts']['experiments'],1)
                self.assertEqual(api('/api/diagnostics/trials/'+e['id'])['scope'],'same_protocol')
                with self.assertRaises(HTTPError) as err:api('/api/diagnostics/reports',dict(experiment_id=e['id'],group_count=9))
                self.assertEqual(err.exception.code,400);err.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join()
                if hasattr(server,'lab'):server.lab.close()


if __name__=='__main__':unittest.main()
