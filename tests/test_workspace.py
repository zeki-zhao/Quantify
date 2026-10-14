import csv
import io
import json
import math
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Handler
from quantify import datasets
from quantify.factors import create_version, calculate
from quantify.research import demo_csv, parse_panel
from quantify.storage import ResearchStore
from quantify.workbench import Workbench


def daily_csv(months=108, tri=True):
    rows = []
    for i in range(months):
        stamp = f'{2015 + i // 12}-{1 + i % 12:02d}-20'
        for stock in range(3):
            close = 20 + i * .1 + stock + math.sin(i + stock)
            rows.append(dict(date=stamp, symbol=f'60000{stock}.SH', open=close, high=close + 1,
                             low=close - 1, close=close, volume=10000,
                             total_return_index=close * 5 if tri else '', available_at=stamp))
    return datasets.csv_text('daily', rows)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workbench = Workbench(self.temp.name)
        self.store = self.workbench.store

    def save(self, kind='daily', raw=None, **changes):
        args = dict(kind=kind, raw_csv=daily_csv(3) if raw is None else raw,
                    source='合成测试 fixture；价格元/股，成交量股',
                    price_basis='unadjusted' if kind == 'daily' else 'not_applicable', is_demo=True)
        args.update(changes)
        return datasets.import_snapshot(self.store, **args)

    def demo(self):
        return self.workbench.post('/api/workspace/demo', {})

    def factor(self, kind='momentum'):
        return next(f for f in self.store.list('factor') if f['kind'] == kind and not f['parent_id'])

    def run_demo(self, **changes):
        args = dict(snapshot_id=self.demo()['id'], factor_ids=[self.factor()['id']])
        args.update(changes)
        return self.workbench.run(**args)

    def test_snapshot_idempotent_increment_and_immutable_revision(self):
        first = self.save()
        again = self.save(parent_id=first['id'])
        self.assertEqual(first['id'], again['id'])
        expanded = self.save(raw=daily_csv(4), parent_id=first['id'])
        self.assertEqual(expanded['row_count'], 12)
        self.assertEqual(self.store.get('snapshot', first['id'])['row_count'], 9)
        modified = daily_csv(4).replace(',10000,', ',20000,', 1)
        revision = self.save(raw=modified, parent_id=expanded['id'])
        self.assertNotEqual(revision['id'], expanded['id'])
        self.assertEqual(revision['rows'][0]['volume'], 20000)
        self.assertEqual(self.store.get('snapshot', expanded['id'])['rows'][0]['volume'], 10000)
        with self.assertRaisesRegex(ValueError, '保持'):
            self.save(parent_id=first['id'], source='another source')

    def test_snapshot_persists_after_reopen_and_checks_digest(self):
        first = self.save()
        self.assertEqual(ResearchStore(self.temp.name).get('snapshot', first['id']), first)
        with self.store.connect() as db:
            db.execute('UPDATE artifacts SET body=? WHERE id=?', ('{}', first['id']))
        with self.assertRaisesRegex(ValueError, '摘要'):
            self.store.get('snapshot', first['id'])

    def test_rejects_bad_daily_data_atomically(self):
        source = daily_csv(1)
        cases = [source + source.splitlines()[1] + '\n', source.replace(',10000,', ',NaN,', 1),
                 source.replace(',10000,', ',-1,', 1), source.replace('2015-01-20', 'not-date', 1),
                 source + 'too,few,columns\n', source.replace(',10000,', ',10000,extra,', 1)]
        for raw in cases:
            with self.subTest(raw=raw[:90]), self.assertRaises(ValueError):
                self.save(raw=raw)
        with self.assertRaisesRegex(ValueError, 'CSV 格式'):
            self.save(raw=source + '"' + 'x' * 140000)
        self.assertEqual(self.store.list('snapshot'), [])

    def test_rejects_invalid_ohlc_and_empty_input(self):
        rows, _ = datasets.parse_rows('daily', daily_csv(1))
        rows[0]['high'] = rows[0]['low'] - 1
        with self.assertRaisesRegex(ValueError, 'OHLC'):
            self.save(raw=datasets.csv_text('daily', rows))
        with self.assertRaises(ValueError):
            self.save(raw=','.join(datasets.SCHEMAS['daily']))

    def test_financial_revisions_are_point_in_time(self):
        header = ','.join(datasets.SCHEMAS['financials']) + '\n'
        first = self.save('financials', header + '2024-12-31,600000.SH,1,2,2025-03-01\n')
        second = self.save('financials', header + '2024-12-31,600000.SH,3,4,2025-04-01\n', parent_id=first['id'])
        self.assertEqual(datasets.as_of_rows(second, '2025-02-28'), [])
        self.assertEqual(datasets.as_of_rows(second, '2025-03-01'), [])
        self.assertEqual(datasets.as_of_rows(second, '2025-03-15')[0]['quality'], 1)
        self.assertEqual(datasets.as_of_rows(second, '2025-04-15')[0]['quality'], 3)
        with self.assertRaisesRegex(ValueError, '修订'):
            self.save('financials', header + '2024-12-31,600000.SH,9,2,2025-03-01\n', parent_id=second['id'])

    def test_universe_events_as_of(self):
        raw = ','.join(datasets.SCHEMAS['universe']) + '\n2025-01-01,600000.SH,1,2024-12-30\n2025-03-01,600000.SH,0,2025-02-25\n'
        snapshot = self.save('universe', raw)
        self.assertEqual(len(datasets.as_of_rows(snapshot, '2025-02-01')), 1)
        self.assertEqual(datasets.as_of_rows(snapshot, '2025-03-01')[-1]['member'], 0)

    def test_daily_to_monthly_and_price_only_research(self):
        daily = self.save(raw=daily_csv())
        monthly = datasets.monthly_from_daily(self.store, daily_id=daily['id'])
        self.assertEqual(monthly['row_count'], 107 * 3)
        self.assertEqual(monthly['derived_from']['daily'], daily['id'])
        self.assertIsNone(monthly['rows'][0]['quality'])
        self.assertEqual(len(self.store.list('snapshot')), 2)
        run = self.run_demo(snapshot_id=monthly['id'])
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertTrue(run['report']['is_demo'])
        failed = self.run_demo(snapshot_id=monthly['id'], factor_ids=[self.factor('quality')['id']])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('财务', failed['error'])

    def test_daily_missing_return_index_or_observation_rejected(self):
        daily = self.save(raw=daily_csv(tri=False))
        with self.assertRaisesRegex(ValueError, '含分红指数'):
            datasets.monthly_from_daily(self.store, daily_id=daily['id'])
        rows, _ = datasets.parse_rows('daily', daily_csv())
        del rows[50]
        incomplete = self.save(raw=datasets.csv_text('daily', rows))
        with self.assertRaisesRegex(ValueError, '不齐全'):
            datasets.monthly_from_daily(self.store, daily_id=incomplete['id'])

    def test_monthly_join_uses_known_financial_period_and_not_later_revision(self):
        daily = self.save(raw=daily_csv())
        rows = []
        for stock in range(3):
            symbol = f'60000{stock}.SH'
            rows += [dict(period_end='2014-06-30', symbol=symbol, quality=1, value=2, available_at='2014-09-01'),
                     dict(period_end='2014-06-30', symbol=symbol, quality=9, value=8, available_at='2015-03-01')]
        finance = self.save('financials', datasets.csv_text('financials', rows))
        monthly = datasets.monthly_from_daily(self.store, daily_id=daily['id'], financials_id=finance['id'])
        self.assertEqual(monthly['rows'][0]['quality'], 1)
        self.assertEqual(monthly['rows'][6]['quality'], 9)

    def test_dynamic_universe_rejected_instead_of_silent_filter(self):
        daily = self.save(raw=daily_csv())
        rows = [dict(date='2014-01-01', symbol=f'60000{i}.SH', member=1, available_at='2014-01-01') for i in range(3)]
        rows.append(dict(date='2016-01-01', symbol='600002.SH', member=0, available_at='2016-01-01'))
        universe = self.save('universe', datasets.csv_text('universe', rows))
        with self.assertRaisesRegex(ValueError, '动态池'):
            datasets.monthly_from_daily(self.store, daily_id=daily['id'], universe_id=universe['id'])

    def test_factor_version_and_past_signal_unchanged_by_future(self):
        parent = self.factor()
        child = create_version(self.store, parent_id=parent['id'], name='12个月动量', reason='更长趋势', lookback=12)
        self.assertNotEqual(parent['id'], child['id'])
        self.assertEqual(self.store.get('factor', parent['id'])['lookback'], 6)
        _, panel = parse_panel(demo_csv())
        before = calculate(panel, 30, [child])
        panel[31]['DEMO1']['total_return_index'] *= 10
        self.assertEqual(before, calculate(panel, 30, [child]))
        with self.assertRaises(ValueError):
            create_version(self.store, parent_id=parent['id'], name='bad', reason='bad', lookback=999)

    def test_repeatable_experiment_and_version_comparison(self):
        left = self.run_demo()
        self.assertEqual(left['status'], 'completed', left.get('error'))
        replay = Workbench(self.temp.name).replay(experiment_id=left['id'])
        self.assertNotEqual(left['id'], replay['id'])
        self.assertEqual(left['report'], replay['report'])
        child = create_version(self.store, parent_id=self.factor()['id'], name='长窗口', reason='检验趋势', lookback=12)
        right = self.run_demo(factor_ids=[child['id']])
        self.assertEqual(right['status'], 'completed', right.get('error'))
        compared = self.workbench.compare(left_id=left['id'], right_id=right['id'])
        self.assertEqual(compared['difference_right_minus_left']['observations'], 0)
        self.assertEqual(left['report']['baseline'], right['report']['baseline'])
        wrong_cost = self.run_demo(cost_bps=50)
        with self.assertRaisesRegex(ValueError, '参数'):
            self.workbench.compare(left_id=left['id'], right_id=wrong_cost['id'])

    def test_holdout_never_reaches_evaluator(self):
        first = self.run_demo()
        snap = self.store.get('snapshot', first['request']['snapshot_id'])
        for row in snap['rows']:
            if row['date'] >= first['protocol']['holdout_start']:
                row['total_return_index'] *= 100
                row['quality'] = 999
        second_snapshot = self.save('monthly', datasets.csv_text('monthly', snap['rows']),
                                    price_basis='total_return_index')
        second = self.run_demo(snapshot_id=second_snapshot['id'])
        self.assertEqual(first['report']['selected'], second['report']['selected'])
        self.assertEqual(first['report']['folds'], second['report']['folds'])
        self.assertLess(first['report']['data_window'][-1], first['protocol']['holdout_start'])
        self.assertNotIn(first['protocol']['holdout_start'], first['report']['dataset']['raw_csv'])

    def test_failures_preserved_and_short_history_not_passed(self):
        failed = self.run_demo(top_k=100)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(self.store.get('experiment', failed['id'])['status'], 'failed')
        with self.assertRaises(ValueError):
            self.workbench.compare(left_id=failed['id'], right_id=failed['id'])
        short = self.run_demo(holdout_months=60)
        self.assertEqual(short['status'], 'failed')


class WorkspaceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.data_dir = cls.temp.name
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        return urlopen(Request(self.base + path, data=data, headers={'Content-Type': 'application/json'}), timeout=20)

    def api(self, path, payload=None):
        with self.request('/api/workspace/' + path, payload) as response:
            return json.load(response)

    def test_workspace_assets_and_full_http_research(self):
        for path in ('/workspace', '/workspace.js', '/workspace.css', '/api/workspace/templates/daily.csv'):
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())
        snapshot = self.api('demo', {})
        parent = next(f for f in self.api('factors') if f['kind'] == 'momentum')
        child = self.api('factors', dict(parent_id=parent['id'], name='新动量', reason='趋势假设', lookback=12))
        run = self.api('experiments', dict(snapshot_id=snapshot['id'], factor_ids=[child['id']]))
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(self.api('experiments/' + run['id'])['report'], run['report'])
        replay = self.api('replay', {'experiment_id': run['id']})
        comparison = self.api('compare', {'left_id': run['id'], 'right_id': replay['id']})
        self.assertEqual(comparison['difference_right_minus_left']['mean_return'], 0)
        self.assertGreaterEqual(len(self.api('experiments')), 2)

    def test_invalid_import_has_no_partial_snapshot(self):
        before = len(self.api('snapshots'))
        with self.assertRaises(HTTPError) as error:
            self.api('import', dict(kind='daily', raw_csv='bad', source='test', price_basis='unadjusted', is_demo=True))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(len(self.api('snapshots')), before)

    def test_nonfinite_json_rejected_and_unknown_route(self):
        with self.assertRaises(HTTPError) as error:
            self.api('experiments', dict(cost_bps=float('nan')))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.api('unknown', {})
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
