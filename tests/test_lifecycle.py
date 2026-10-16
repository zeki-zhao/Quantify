import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

from quantify import datasets, factors
from quantify.lab import Lab
from quantify.lifecycle import Lifecycle
from quantify.objectives import policy, assess
from quantify.research import evaluate, demo_csv
from quantify.workbench import Workbench
from test_lab import proposal, wait_job


def trend_rows(months=108, prefix='S'):
    rows = []
    for month in range(months):
        stamp = f'{2017+month//12}-{month%12+1:02d}-01'
        for i in range(5):
            rows.append(dict(date=stamp, symbol=f'{prefix}{i}', total_return_index=100*(1.02 if i < 2 else .99)**month,
                             quality=5-i, value=i, available_at=stamp))
    return rows


def import_rows(store, rows, source='合成单调流程数据'):
    return datasets.import_snapshot(store, kind='monthly', raw_csv=datasets.csv_text('monthly', rows),
                                    source=source, price_basis='total_return_index', is_demo=True)


class ObjectiveTests(unittest.TestCase):
    def test_audited_positive_rate_regression_is_rejected_with_reason(self):
        baseline = dict(positive_rate=.6875, mean_return=.0348066944, worst_return=-.1734123975, observations=16)
        candidate = dict(positive_rate=.625, mean_return=.0376363468, worst_return=-.1132283939, observations=16)
        decision = assess(candidate, baseline)
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['reasons'], ['主目标未超过最低改善幅度'])
        self.assertTrue(assess(candidate, baseline, {'primary': 'mean_return'})['accepted'])
        for changes, reason in [({'positive_rate': .8, 'mean_return': .01}, '平均收益低于允许下限'),
                                ({'positive_rate': .8, 'worst_return': -.3}, '最差单笔收益低于允许下限'),
                                ({'positive_rate': .8, 'observations': 0}, '样本量不足')]:
            self.assertIn(reason, assess({**candidate, **changes}, baseline)['reasons'])
        with self.assertRaises(ValueError): policy({'primary': 'goal text'})
        with self.assertRaises(ValueError): policy({'min_observations': True})

    def test_training_and_session_share_policy_and_lineage_tracks_best(self):
        with tempfile.TemporaryDirectory() as temp:
            lab = Lab(temp)
            try:
                w = lab.workbench
                snapshot = w.post('/api/workspace/demo', {})
                momentum = next(f for f in w.store.list('factor') if f['kind'] == 'momentum')
                baseline = w.run(snapshot_id=snapshot['id'], factor_ids=[momentum['id']], objective={'primary': 'mean_return'})
                session = lab.create_session(baseline_id=baseline['id'], goal='上涨比例的文字不会覆盖明确选择的均值目标', max_rounds=2)
                lab.store.append_proposals(session['id'], [proposal('-volatility(6,0)'), proposal('rank(momentum(12,1))-rank(volatility(6,0))')])
                job = wait_job(lab.store, session['id'])
                outcomes = job['progress']['outcomes']
                self.assertEqual(outcomes[0]['status'], 'improved')
                child = w.store.get('factor', outcomes[1]['factor_id'])
                self.assertEqual(child['parent_id'], outcomes[0]['factor_id'])
                for outcome in outcomes:
                    experiment = w.store.get('experiment', outcome['experiment_id'])
                    self.assertEqual(experiment['report']['objective'], session['payload']['objective'])
                self.assertEqual(lab.research_package(session['id'])['context']['objective']['primary'], 'mean_return')
                positive = w.run(snapshot_id=snapshot['id'], factor_ids=[momentum['id']])
                session2 = lab.create_session(baseline_id=positive['id'], goal='提高上涨比例', max_rounds=2)
                lab.store.append_proposals(session2['id'], [proposal('-volatility(6,0)'), proposal('rank(momentum(12,1))-rank(volatility(6,0))')])
                job2 = wait_job(lab.store, session2['id'])
                self.assertEqual([o['status'] for o in job2['progress']['outcomes']], ['not_improved', 'not_improved'])
                with self.assertRaisesRegex(ValueError, '参数必须相同'):
                    w.compare(left_id=baseline['id'], right_id=positive['id'])
            finally:
                lab.close()

    def test_training_replacement_obeys_guards_and_keeps_common_sample(self):
        objective = policy({'primary': 'mean_return', 'max_mean_deterioration': .01})
        report = evaluate(demo_csv(), factor_specs=list(factors.BUILTINS), objective=objective)
        for fold in report['folds']:
            chosen = fold['chosen_factor']
            if chosen != factors.BUILTINS[0]['id']:
                self.assertTrue(fold['training_decisions'][chosen]['accepted'])
                self.assertEqual(fold['training_decisions'][chosen]['objective'], objective)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.w = Workbench(self.temp.name); self.store = self.w.store; self.service = Lifecycle(self.w)
        self.rows = trend_rows()
        self.snap = import_rows(self.store, self.rows)
        self.factor = next(f for f in self.store.list('factor') if f['kind'] == 'quality')
        self.experiment = self.w.run(snapshot_id=self.snap['id'], factor_ids=[self.factor['id']])
        self.assertEqual(self.experiment['status'], 'completed')

    def freeze(self, **changes):
        return self.service.freeze(experiment_id=self.experiment['id'], **changes)

    def activate(self):
        frozen = self.freeze()
        result = self.service.evaluate(freeze_id=frozen['id'])
        self.assertTrue(result['accepted'], result)
        self.service.change(action='activate', expected_active=None, freeze_id=frozen['id'], reason='合成流程验证')
        return frozen

    def test_freeze_fixed_factor_final_only_and_cached_repeat(self):
        frozen = self.freeze()
        with patch('quantify.lifecycle.picked', wraps=__import__('quantify.lifecycle', fromlist=['picked']).picked) as calculate:
            result = self.service.evaluate(freeze_id=frozen['id'])
        self.assertTrue(result['independent']); self.assertTrue(result['accepted'])
        self.assertEqual(result['selected']['positive_rate'], 1)
        self.assertEqual(result['reference']['positive_rate'], .4)
        self.assertEqual(result['selected']['observations'], 6)
        self.assertEqual({call.args[2]['id'] for call in calculate.call_args_list if call.args[2]}, {self.factor['id']})
        for window in result['windows']:
            self.assertGreaterEqual(window['signal'], frozen['holdout_start'])
            self.assertLess(window['signal'], window['entry']); self.assertLess(window['entry'], window['exit'])
        self.assertEqual(self.service.evaluate(freeze_id=frozen['id']), result)
        self.assertIsNone(self.service.state()['active_id'])
        self.assertTrue(result['is_demo'])
        self.assertFalse(self.experiment['protocol']['holdout_evaluated'])

    def test_retuning_revisions_sources_and_simultaneous_freezes_do_not_reset_exposure(self):
        first = self.freeze(name='第一版')
        second = self.freeze(name='看结果前冻结第二版')
        self.service.evaluate(freeze_id=first['id'])
        result = self.service.evaluate(freeze_id=second['id'])
        self.assertFalse(result['independent']); self.assertFalse(result['accepted'])
        revised = copy.deepcopy(self.rows); revised[-1]['total_return_index'] *= 1.1
        snapshot = import_rows(self.store, revised, source='换来源或改历史也不能清除暴露')
        experiment = self.w.run(snapshot_id=snapshot['id'], factor_ids=[self.factor['id']])
        frozen = self.service.freeze(experiment_id=experiment['id'])
        result = self.service.evaluate(freeze_id=frozen['id'])
        self.assertFalse(result['independent']); self.assertIn(first['id'], result['previous_exposures'])
        with self.assertRaisesRegex(ValueError, '独立最终验收'):
            self.service.change(action='activate', expected_active=None, freeze_id=frozen['id'], reason='不得启用')
        reopened = Lifecycle(Workbench(self.temp.name))
        self.assertEqual(reopened.evaluate(freeze_id=first['id'])['id'], first['id'])

    def test_failure_and_interrupted_reservation_consume_holdout(self):
        experiment = self.w.run(snapshot_id=self.snap['id'], factor_ids=[self.factor['id']], horizon=6)
        frozen = self.service.freeze(experiment_id=experiment['id'])
        result = self.service.evaluate(freeze_id=frozen['id'])
        self.assertEqual(result['status'], 'failed'); self.assertFalse(result['accepted'])
        later = self.freeze(name='失败之后重测')
        self.assertFalse(self.service.evaluate(freeze_id=later['id'])['independent'])
        crashed = self.freeze(name='模拟进程中断')
        with patch('quantify.lifecycle.panel_for', side_effect=RuntimeError('process crashed')):
            with self.assertRaises(RuntimeError): self.service.evaluate(freeze_id=crashed['id'])
        with self.assertRaisesRegex(ValueError, '已开始验收'):
            self.service.evaluate(freeze_id=crashed['id'])

    def test_manual_replacement_retirement_rollback_append_events(self):
        first = self.activate()
        fresh = import_rows(self.store, trend_rows(prefix='OTHER'))
        experiment = self.w.run(snapshot_id=fresh['id'], factor_ids=[self.factor['id']])
        second = self.service.freeze(experiment_id=experiment['id'])
        self.assertTrue(self.service.evaluate(freeze_id=second['id'])['accepted'])
        with self.assertRaisesRegex(ValueError, '已改变'):
            self.service.change(action='activate', expected_active=None, freeze_id=second['id'], reason='过期页面')
        self.service.change(action='activate', expected_active=first['id'], freeze_id=second['id'], reason='人工替换')
        self.service.change(action='rollback', expected_active=second['id'], freeze_id=first['id'], reason='回滚')
        self.service.change(action='retire', expected_active=first['id'], reason='停止观察新信号')
        state = self.service.state()
        self.assertIsNone(state['active_id'])
        self.assertEqual([event['action'] for event in state['events']], ['activate', 'activate', 'rollback', 'retire'])
        self.assertEqual(state['events'][1]['previous_id'], first['id'])
        self.assertEqual(state, Lifecycle(Workbench(self.temp.name)).state())

    def test_forward_generated_before_entry_only_mature_nonoverlapping_labels(self):
        with patch('quantify.lifecycle.now', return_value=datetime(2026,1,15,tzinfo=timezone.utc)):
            frozen = self.activate()
            january = import_rows(self.store, trend_rows(109))
            signal = self.service.signal(snapshot_id=january['id'])
            self.assertEqual(signal['symbols'], ['S0', 'S1'])
            self.assertEqual(signal, self.service.signal(snapshot_id=january['id']))
            report = self.service.observe(strategy_id=frozen['id'], snapshot_id=january['id'])
            self.assertEqual(report['pending_labels'], 1); self.assertEqual(report['evaluated_windows'], 0)
        with patch('quantify.lifecycle.now', return_value=datetime(2026,4,15,tzinfo=timezone.utc)):
            april = import_rows(self.store, trend_rows(112))
            self.service.signal(snapshot_id=april['id'])
            report = self.service.observe(strategy_id=frozen['id'], snapshot_id=april['id'])
            self.assertEqual(report['evaluated_windows'], 0)
        with patch('quantify.lifecycle.now', return_value=datetime(2026,5,15,tzinfo=timezone.utc)):
            may = import_rows(self.store, trend_rows(113))
            report = self.service.observe(strategy_id=frozen['id'], snapshot_id=may['id'])
            self.assertEqual(report['evaluated_windows'], 1)
            self.assertEqual(report['state'], 'insufficient_mature_labels')
            self.assertEqual(report['labels'][0]['entry'], '2026-02-01')
            self.assertEqual(report['labels'][0]['exit'], '2026-05-01')
        with patch('quantify.lifecycle.now', return_value=datetime(2026,8,15,tzinfo=timezone.utc)):
            august = import_rows(self.store, trend_rows(116))
            report = self.service.observe(strategy_id=frozen['id'], snapshot_id=august['id'])
            self.assertEqual(report['evaluated_windows'], 2); self.assertEqual(report['pending_labels'], 0)
            self.assertEqual(report['state'], 'observing'); self.assertFalse(report['degradation'])
            self.assertEqual(report['selected']['positive_rate'], 1)
            self.assertEqual(report, self.service.observe(strategy_id=frozen['id'], snapshot_id=august['id']))
            self.service.change(action='retire', expected_active=frozen['id'], reason='退役仍保留观察')
            self.assertEqual(report, self.service.forward_report(frozen['id']))
            with self.assertRaisesRegex(ValueError, '先人工启用'): self.service.signal(snapshot_id=august['id'])

    def test_forward_rejects_stale_future_revised_and_retroactive_entry(self):
        with patch('quantify.lifecycle.now', return_value=datetime(2026,1,15,tzinfo=timezone.utc)):
            frozen = self.activate()
            for snapshot in (self.snap, import_rows(self.store, trend_rows(110))):
                with self.assertRaisesRegex(ValueError, '最新截面'): self.service.signal(snapshot_id=snapshot['id'])
            revised = trend_rows(109); revised[0]['total_return_index'] *= 2
            with self.assertRaisesRegex(ValueError, '修订'):
                self.service.signal(snapshot_id=import_rows(self.store, revised)['id'])
        with patch('quantify.lifecycle.now', return_value=datetime(2026,2,10,tzinfo=timezone.utc)):
            january = import_rows(self.store, trend_rows(109))
            self.service.signal(snapshot_id=january['id'])
        with patch('quantify.lifecycle.now', return_value=datetime(2026,5,15,tzinfo=timezone.utc)):
            may = import_rows(self.store, trend_rows(113))
            with self.assertRaisesRegex(ValueError, '事后前向'):
                self.service.observe(strategy_id=frozen['id'], snapshot_id=may['id'])
            self.assertEqual(self.service.forward_report(frozen['id'])['evaluated_windows'], 0)

    def test_concurrent_final_attempts_share_atomic_exposure_ledger(self):
        first, second = self.freeze(name='并发一'), self.freeze(name='并发二')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda f: self.service.evaluate(freeze_id=f['id']), [first, second]))
        self.assertEqual(sum(result['independent'] for result in results), 1)
        self.assertEqual(sum(result['accepted'] for result in results), 1)

    def test_price_final_validation_preserves_basis(self):
        rows = [{**{k: v for k, v in row.items() if k != 'total_return_index'}, 'close': row['total_return_index']} for row in self.rows]
        snapshot = datasets.import_snapshot(self.store, kind='monthly_price', raw_csv=datasets.csv_text('monthly_price', rows),
                       source='合成未复权价格', price_basis='unadjusted', is_demo=True)
        experiment = self.w.run(snapshot_id=snapshot['id'], factor_ids=[self.factor['id']])
        frozen = self.service.freeze(experiment_id=experiment['id'])
        result = self.service.evaluate(freeze_id=frozen['id'])
        self.assertTrue(result['accepted']); self.assertEqual(result['price_basis'], 'unadjusted')
        self.assertTrue(result['snapshot_warnings'])

    def test_forward_degradation_waits_for_two_mature_windows(self):
        def future_rows(months):
            rows = trend_rows(months)
            for row in rows:
                if row['date'] >= '2026-02-01':
                    month = int(row['date'][5:7])
                    row['total_return_index'] *= .5 ** (month - 1)
            return rows
        with patch('quantify.lifecycle.now', return_value=datetime(2026,1,15,tzinfo=timezone.utc)):
            frozen = self.activate()
            self.service.signal(snapshot_id=import_rows(self.store, future_rows(109))['id'])
        with patch('quantify.lifecycle.now', return_value=datetime(2026,4,15,tzinfo=timezone.utc)):
            self.service.signal(snapshot_id=import_rows(self.store, future_rows(112))['id'])
        with patch('quantify.lifecycle.now', return_value=datetime(2026,5,15,tzinfo=timezone.utc)):
            report = self.service.observe(strategy_id=frozen['id'], snapshot_id=import_rows(self.store, future_rows(113))['id'])
            self.assertFalse(report['degradation']); self.assertEqual(report['evaluated_windows'], 1)
        with patch('quantify.lifecycle.now', return_value=datetime(2026,8,15,tzinfo=timezone.utc)):
            report = self.service.observe(strategy_id=frozen['id'], snapshot_id=import_rows(self.store, future_rows(116))['id'])
            self.assertTrue(report['degradation']); self.assertEqual(report['selected']['positive_rate'], 0)
            self.assertEqual(self.service.state()['active_id'], frozen['id'])  # No automatic replacement.
