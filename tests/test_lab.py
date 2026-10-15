import copy
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from quantify.expressions import validate_expression, evaluate_expression
from quantify.factors import calculate, BUILTINS
from quantify.jobs import JobStore, QueueManager, LostLease
from quantify.lab import Lab, initial_progress
from quantify.proposals import parse_proposals
from quantify.research import demo_csv, parse_panel
from quantify.research_tools import ResearchTools
from quantify.workbench import Workbench


def proposal(expression='momentum(6, 0)', **changes):
    return dict(action='propose', name='测试因子', expression=expression, rationale='合成流程验证', **changes)


def wait_job(store, job_id, states=('succeeded', 'failed', 'waiting_input', 'cancelled', 'paused', 'timed_out'), seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = store.job(job_id)
        if job['status'] in states:
            return job
        time.sleep(.025)
    raise AssertionError(f'job did not finish: {store.job(job_id)}')


class ExpressionTests(unittest.TestCase):
    def test_whitelist_rejects_code_future_and_resource_excess(self):
        for expression in ['__import__("os").system("id")', 'quality.__class__', '[x for x in value]',
                           'momentum(6,-1)', 'momentum(25,0)', 'momentum(6,True)', 'momentum(6,skip=0)',
                           '9' * 400, 'quality[0]', 'quality**2', 'sum(value)', '1e999', 'True', '1+' * 100 + '1',
                           'rank(' * 20 + 'quality' + ')' * 20]:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                validate_expression(expression)
        self.assertEqual(validate_expression('rank(quality) + momentum(24,6)')['history_months'], 30)
        self.assertEqual(validate_expression('rank(quality) + value')['required_fields'], ['quality', 'value'])

    def test_equivalent_builtin_and_future_invariance(self):
        _, panel = parse_panel(demo_csv())
        baseline = calculate(panel, 30, [BUILTINS[0]])
        actual = evaluate_expression('momentum(6,0)', panel, 30)
        self.assertEqual(actual, {symbol: values['momentum'] for symbol, values in baseline.items()})
        altered = copy.deepcopy(panel)
        for month in altered[31:]:
            for row in month.values():
                row.update(total_return_index=999999, quality=987654, value=-999)
        formula = 'rank(momentum(12,1)) - zscore(volatility(6,0)) + rank(quality)'
        self.assertEqual(evaluate_expression(formula, panel, 30), evaluate_expression(formula, altered, 30))

    def test_numeric_and_missing_field_failures(self):
        _, panel = parse_panel(demo_csv())
        for expression in ['1 / (quality-quality)', '1000000 * 1000000 * 2']:
            with self.assertRaises(ValueError): evaluate_expression(expression, panel, 30)
        self.assertEqual(set(evaluate_expression('zscore(1)', panel, 30).values()), {0.})
        self.assertEqual(set(evaluate_expression('rank(1)', panel, 30).values()), {.5})
        next(iter(panel[30].values()))['quality'] = None
        with self.assertRaisesRegex(ValueError, '缺少'): evaluate_expression('quality', panel, 30)

    def test_proposal_schema(self):
        valid = proposal()
        self.assertEqual(parse_proposals('```json\n'+json.dumps(valid)+'\n```'), [valid])
        for value in [[], [valid]*11, {**valid, 'python': 'print(1)'}, {**valid, 'expression': 2}, 'NaN', '{broken']:
            with self.subTest(value=value), self.assertRaises(ValueError): parse_proposals(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = JobStore(self.temp.name)

    def test_recovery_and_lease_keep_history(self):
        running = self.store.create('experiment', {}, 10)
        self.store.claim('old')
        queued = self.store.create('experiment', {}, 10)
        waiting = self.store.create('research', {'proposals': []}, 10, status='waiting_input')
        self.store.recover()
        for job in (running, queued): self.assertEqual(self.store.job(job['id'])['status'], 'interrupted')
        self.assertEqual(self.store.job(waiting['id'])['status'], 'waiting_input')
        with self.assertRaises(LostLease): self.store.checkpoint(running['id'], 'old', {})
        with self.assertRaises(LostLease): self.store.finish(running['id'], 'old', 'succeeded')
        self.assertEqual(self.store.events(running['id'])[-1]['event'], 'interrupted')
        self.assertEqual(JobStore(self.temp.name).job(queued['id'])['status'], 'interrupted')

    def test_stop_request_wins_completion_and_budget_survives_pause(self):
        job = self.store.create('research', {'proposals': [], 'max_rounds': 3}, 10)
        self.store.claim('owner')
        self.store.control(job['id'], 'pause')
        self.store.finish(job['id'], 'owner', 'succeeded')
        self.store.add_elapsed(job['id'], 2)
        self.assertEqual(self.store.job(job['id'])['status'], 'paused')
        self.assertEqual(self.store.control(job['id'], 'resume')['status'], 'waiting_input')
        self.assertEqual(self.store.job(job['id'])['elapsed_seconds'], 2)
        self.store.control(job['id'], 'cancel')
        with self.assertRaises(ValueError): self.store.control(job['id'], 'resume')

    def test_queue_owner_and_actual_process_timeout_cancel(self):
        processes = []
        def spawn(manager, job):
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            processes.append(process)
            return process
        with patch.object(QueueManager, '_spawn', spawn):
            manager = QueueManager(self.temp.name)
            try:
                with self.assertRaisesRegex(ValueError, '已有'): QueueManager(self.temp.name)
                timed = manager.store.create('experiment', {}, .2)
                self.assertEqual(wait_job(self.store, timed['id'])['status'], 'timed_out')
                cancelled = manager.store.create('experiment', {}, 20)
                wait_job(self.store, cancelled['id'], ('running',))
                self.store.control(cancelled['id'], 'cancel')
                self.assertEqual(wait_job(self.store, cancelled['id'])['status'], 'cancelled')
                # Running tasks stop when the owning service shuts down.
                closing = manager.store.create('experiment', {}, 20)
                wait_job(self.store, closing['id'], ('running',))
            finally:
                manager.close()
            self.assertEqual(self.store.job(closing['id'])['status'], 'interrupted')
        self.assertTrue(processes)
        self.assertTrue(all(p.poll() is not None for p in processes))
        reopened = QueueManager(self.temp.name); reopened.close()


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.lab = Lab(self.temp.name); self.addCleanup(self.lab.close)
        self.workbench = self.lab.workbench; self.store = self.lab.store
        snapshot = self.workbench.post('/api/workspace/demo', {})
        factor = next(f for f in self.store.list('factor') if f['kind'] == 'momentum')
        self.request = dict(snapshot_id=snapshot['id'], factor_ids=[factor['id']])
        self.baseline = self.workbench.run(**self.request)
        self.assertEqual(self.baseline['status'], 'completed')

    def session(self, **changes):
        return self.lab.create_session(baseline_id=self.baseline['id'], goal='仅测试流程', **changes)

    def submit(self, job, proposals):
        self.lab.post('/api/lab/proposals', {'job_id': job['id'], 'proposals': proposals})
        return wait_job(self.store, job['id'])

    def test_experiment_queue_success_failure_and_retry(self):
        job = self.lab.submit_experiment(request=self.request)
        completed = wait_job(self.store, job['id'])
        self.assertEqual(completed['status'], 'succeeded')
        result = self.store.get('experiment', completed['result']['experiment_id'])
        self.assertEqual(result['report']['selected'], self.baseline['report']['selected'])
        failed = self.lab.submit_experiment(request={**self.request, 'top_k': 0})
        result = wait_job(self.store, failed['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.store.get('experiment', result['result']['experiment_id'])['status'], 'failed')
        retried = self.lab.retry(failed['id'])
        self.assertNotEqual(retried['id'], failed['id'])
        self.assertEqual(retried['retry_of'], failed['id'])
        self.assertEqual(wait_job(self.store, retried['id'])['status'], 'failed')

    def test_manual_feedback_loop_invalid_duplicate_and_round_budget(self):
        session = self.session(max_rounds=3, patience=3)
        self.assertEqual(session['status'], 'waiting_input')
        first = self.submit(session, proposal())
        self.assertEqual(first['status'], 'waiting_input')
        self.assertEqual(first['progress']['outcomes'][0]['status'], 'not_improved')
        second = self.submit(session, [proposal('__import__("os")'), proposal('momentum(6,0)')])
        self.assertEqual(second['status'], 'succeeded')
        self.assertEqual(second['result']['reason'], 'budget_exhausted')
        self.assertEqual(second['progress']['candidates'], 1)
        self.assertEqual([o['status'] for o in second['progress']['outcomes']], ['not_improved', 'rejected', 'rejected'])
        self.assertEqual(second['progress']['best_id'], self.baseline['id'])
        self.assertFalse(second['result']['activated'])
        events = self.store.events(session['id'])
        self.assertEqual(sum(e['event'] == 'proposal' for e in events), 3)
        self.assertTrue(any(e['event'] == 'tool' for e in events))
        with self.assertRaises(ValueError): self.submit(session, proposal())

    def test_scope_and_package_exclude_raw_and_holdout(self):
        session = self.session()
        package = self.lab.research_package(session['id'])
        encoded = json.dumps(package)
        self.assertNotIn('raw_csv', encoded)
        self.assertNotIn('raw_import', encoded)
        self.assertNotIn('DEMO1', encoded)
        self.assertFalse(package['context']['baseline']['protocol']['holdout_evaluated'])
        self.assertLess(package['context']['baseline']['data_window'][1], self.baseline['protocol']['holdout_start'])
        tools = ResearchTools(self.temp.name, self.baseline['id'], session['progress'])
        with self.assertRaises(ValueError): tools.call('read_snapshot')
        with self.assertRaises(ValueError): tools.call('get_experiment_result', experiment_id='foreign')
        with self.assertRaises(ValueError): tools.call('get_factor_version', factor_id='foreign')
        # Changing ONLY holdout values must leave development outputs identical.
        snapshot = self.store.get('snapshot', self.request['snapshot_id'])
        from quantify import datasets
        for row in snapshot['rows']:
            if row['date'] >= self.baseline['protocol']['holdout_start']:
                row['total_return_index'] = 987654321
        changed = datasets.import_snapshot(self.store, kind='monthly', raw_csv=datasets.csv_text('monthly', snapshot['rows']),
                    source='holdout sentinel', price_basis='total_return_index', is_demo=True)
        rerun = self.workbench.run(**{**self.request, 'snapshot_id': changed['id']})
        self.assertEqual(rerun['report']['selected'], self.baseline['report']['selected'])
        job = self.lab.create_session(baseline_id=rerun['id'], goal='隔离验证')
        self.assertNotIn('987654321', json.dumps(self.lab.research_package(job['id'])))

    def test_pause_resume_restart_and_stop_proposal(self):
        session = self.session()
        self.store.control(session['id'], 'pause')
        self.store.append_proposals(session['id'], parse_proposals(proposal()))
        self.lab.close()
        self.lab = Lab(self.temp.name); self.addCleanup(self.lab.close); self.store = self.lab.store
        self.assertEqual(self.store.job(session['id'])['status'], 'paused')
        self.store.control(session['id'], 'resume')
        self.assertEqual(wait_job(self.store, session['id'])['status'], 'waiting_input')
        result = self.submit(session, {'action': 'stop', 'name': '', 'expression': '', 'rationale': '证据不足，停止'})
        self.assertEqual(result['result']['reason'], 'proposal_stop')
        self.assertEqual(len(result['progress']['outcomes']), 2)

    def test_candidate_patience_and_import_budgets(self):
        session = self.session(max_candidates=1)
        result = self.submit(session, [proposal(), proposal('momentum(12,0)')])
        self.assertEqual(result['progress']['rounds'], 1)
        self.assertEqual(result['result']['reason'], 'budget_exhausted')
        session = self.session(patience=1)
        result = self.submit(session, proposal())
        self.assertEqual(result['result']['reason'], 'no_improvement')
        session = self.session(max_rounds=1)
        with self.assertRaisesRegex(ValueError, '预算'): self.submit(session, [proposal(), proposal()])
        self.assertEqual(self.store.job(session['id'])['payload']['proposals'], [])

    def test_engine_change_rejected(self):
        session = self.session()
        with patch('quantify.research_tools.engine_fingerprint', return_value={}):
            with self.assertRaisesRegex(ValueError, '旧引擎'): self.session()
        self.store.control(session['id'], 'cancel')
        retried = self.lab.retry(session['id'])
        self.assertEqual(retried['status'], 'waiting_input')
        self.assertEqual(retried['progress']['rounds'], 0)

    def test_improvement_threshold_and_tail_guard_produce_only_suggestion(self):
        session = self.session(max_rounds=1)
        result = self.submit(session, proposal('-volatility(6,0)'))
        self.assertEqual(result['progress']['outcomes'][0]['status'], 'improved')
        suggestion = self.store.get('research_proposal', result['result']['suggestion_id'])
        self.assertEqual(suggestion['status'], 'research_suggestion_only')
        self.assertFalse(result['result']['activated'])
        strict = self.session(max_rounds=1, min_mean_improvement=.1)
        rejected = self.submit(strict, proposal('-volatility(6,0)'))
        self.assertEqual(rejected['progress']['outcomes'][0]['status'], 'not_improved')
        # Positive mean delta alone must not pass a tail constraint.
        value = next(f for f in self.store.list('factor') if f['kind'] == 'value')
        baseline = self.workbench.run(**{**self.request, 'factor_ids': [value['id']]})
        tail = self.lab.create_session(baseline_id=baseline['id'], goal='尾部约束', max_rounds=1)
        result = self.submit(tail, proposal('-volatility(6,0)'))
        outcome = result['progress']['outcomes'][0]
        self.assertGreater(outcome['comparison']['difference_right_minus_left']['mean_return'], 0)
        self.assertEqual(outcome['status'], 'not_improved')
        self.assertIsNone(result['result']['suggestion_id'])

    def test_resume_pending_round_reuses_committed_experiment(self):
        session = self.session(max_rounds=1)
        self.lab.close()
        self.store.append_proposals(session['id'], parse_proposals(proposal()))
        claimed = self.store.claim('fixture')
        progress = claimed['progress']
        tools = ResearchTools(self.temp.name, self.baseline['id'], progress, session_id=session['id'])
        candidate = tools.call('create_factor_candidate', name='测试因子', expression='momentum(6,0)', rationale='合成流程验证')
        experiment_id = session['id'] + '-round-1'
        tools.call('run_experiment', factor_id=candidate['id'], experiment_id=experiment_id)
        # Simulate interruption after artifact commit, before round result checkpoint.
        progress.update(cursor=1, rounds=1, candidates=1,
                        pending=dict(proposal=proposal(), round=1, stage='validated', factor_id=candidate['id'],
                                     normalized=validate_expression('momentum(6,0)')['normalized'], experiment_id=experiment_id))
        self.store.checkpoint(session['id'], 'fixture', progress)
        self.store.finish(session['id'], 'fixture', 'paused')
        self.lab = Lab(self.temp.name); self.addCleanup(self.lab.close); self.store = self.lab.store
        self.store.control(session['id'], 'resume')
        result = wait_job(self.store, session['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['progress']['rounds'], 1)
        self.assertEqual(len(result['progress']['outcomes']), 1)
        self.assertEqual(len(self.store.list('experiment')), 2)
