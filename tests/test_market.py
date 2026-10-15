import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from quantify import datasets
from quantify.jobs import JobStore
from quantify.market import MarketStore, MarketService, closed_day, download_history, history_request
from quantify.market_providers import (CHINA, EastmoneyProvider, SinaProvider, ProviderError, PublicHTTP,
                                       SINA_URL, normalize_symbol, parse_sina, parse_eastmoney)
from quantify.research import parse_panel, demo_csv
from quantify.research_tools import ResearchTools
from quantify.lab import initial_progress
from quantify.workbench import Workbench
from quantify.worker import execute
from test_workspace import daily_csv

NOW = datetime(2026, 10, 15, 10, 0, 0, tzinfo=CHINA)


def sina_row(code='sh600519', price='12', stamp='2026-10-15', clock='10:00:00'):
    values = ['合成接口测试', '11', '10', price, '13', '9', '0', '0', '10000', '120000'] + ['0'] * 20 + [stamp, clock, '00']
    return ('var hq_str_' + code + '="' + ','.join(values) + '";').encode('gbk')


def eastmoney_raw(code='600519', records=None, market=1):
    return json.dumps({'rc': 0, 'data': {'code': code, 'market': market, 'klines': records or [
        '2026-10-13,10,11,12,9,123,135300,0,0,0,0',
        '2026-10-14,11,12,13,10,125,150000,0,0,0,0']}}).encode()


class Transport:
    def __init__(self, response): self.response, self.calls = response, []
    def get(self, url, headers):
        self.calls.append((url, headers))
        return self.response


class HistoryFixture:
    def __init__(self, fail=None, changed=False): self.fail, self.changed, self.calls = fail, changed, []
    def history(self, symbol, start_date, end_date):
        self.calls.append((symbol, start_date, end_date))
        if symbol == self.fail: raise ProviderError('no_data', '合成空响应')
        rows = [dict(date='2026-10-13', symbol=symbol, open=10, close=11, high=12, low=9,
                     volume=10000, total_return_index=None, available_at='2026-10-13'),
                dict(date='2026-10-14', symbol=symbol, open=11, close=12 if not self.changed else 12.5, high=13, low=10,
                     volume=10000, total_return_index=None, available_at='2026-10-14')]
        # Match normal provider normalization: optional CSV field is serialized as blank.
        for row in rows: row['total_return_index'] = ''
        return {'rows': rows, 'raw_sha256': 'fixture-hash'}


class ProviderTests(unittest.TestCase):
    def test_symbol_mapping_and_rejection(self):
        for raw, expected in [('600519', '600519.SH'), ('sz000001', '000001.SZ'), ('920001.BJ', '920001.BJ')]:
            self.assertEqual(normalize_symbol(raw), expected)
        for raw in ['600519.SZ', '000001.SH', 'https://test', 'AAPL', '__import__', 1]:
            with self.assertRaises(ValueError): normalize_symbol(raw)

    def test_sina_code_mapping_partial_errors_and_times(self):
        raw = sina_row('sz000001') + sina_row('sh600519')
        batch = parse_sina(raw, ['600519.SH', '000001.SZ'], NOW)
        self.assertEqual(batch['quotes']['600519.SH']['price'], 12)
        self.assertEqual(batch['quotes']['600519.SH']['volume'], 10000)
        self.assertEqual(batch['quotes']['600519.SH']['quote_at'], NOW.isoformat())
        partial = parse_sina(sina_row() + b'var hq_str_sz000001="";', ['600519.SH', '000001.SZ'], NOW)
        self.assertEqual(list(partial['quotes']), ['600519.SH'])
        self.assertIn('000001.SZ', partial['errors'])
        for raw in [sina_row(price='NaN'), sina_row(price='0'), sina_row(clock='10:10:00')]:
            self.assertFalse(parse_sina(raw, ['600519.SH'], NOW)['quotes'])

    def test_sina_never_executes_response_or_accepts_duplicate(self):
        for raw in [b'<html>forbidden</html>', sina_row() + b'process.exit()', sina_row() * 2]:
            with self.assertRaises(ProviderError): parse_sina(raw, ['600519.SH'], NOW)
        transport = Transport(sina_row())
        provider = SinaProvider(transport, clock=lambda: NOW)
        self.assertEqual(provider.quotes(['600519.SH'])['quotes']['600519.SH']['unit'], 'CNY/share')
        url, headers = transport.calls[0]
        self.assertIn('list=sh600519', url)
        self.assertIn('Referer', headers)

    def test_eastmoney_dates_units_and_identity(self):
        response = parse_eastmoney(eastmoney_raw(), '600519.SH', '2026-10-13', '2026-10-14')
        self.assertEqual(response['rows'][0]['volume'], 12300)
        self.assertIsNone(response['rows'][0]['total_return_index'])
        self.assertEqual(response['rows'][0]['close'], 11.)
        transport = Transport(eastmoney_raw())
        EastmoneyProvider(transport).history('600519.SH', '2026-10-13', '2026-10-14')
        self.assertIn('fqt=0', transport.calls[0][0])
        for raw in [b'not JSON', eastmoney_raw(code='600036'), eastmoney_raw(market=0),
                    eastmoney_raw(records=['2026-10-12,10,11,12,9,100,0']),
                    eastmoney_raw(records=['2026-10-13,10,11,12,9,100,0'] * 2)]:
            with self.assertRaises(ValueError): parse_eastmoney(raw, '600519.SH', '2026-10-13', '2026-10-14')

    def test_network_policy_and_retry_are_bounded(self):
        transport = PublicHTTP()
        transport.opener = Mock()
        transport.opener.open.side_effect = URLError('Tunnel connection failed: 403 Forbidden')
        with self.assertRaises(ProviderError) as error: transport.get(SINA_URL, {})
        self.assertEqual(error.exception.category, 'network_policy')
        self.assertEqual(transport.opener.open.call_count, 1)
        transport.opener.open.reset_mock()
        transport.opener.open.side_effect = HTTPError(SINA_URL, 429, 'rate limited', {}, None)
        with patch('quantify.market_providers.time.sleep'), self.assertRaises(ProviderError): transport.get(SINA_URL, {})
        self.assertEqual(transport.opener.open.call_count, 2)
        with self.assertRaises(ProviderError): transport.get('https://untrusted.example/', {})


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MarketStore(self.temp.name); self.jobs = JobStore(self.temp.name)
        self.service = MarketService(self.temp.name, self.jobs, clock=lambda: NOW, start_thread=False)
        self.addCleanup(self.service.close)
        self.config = self.store.configure(symbols=['600519.SH', '000001.SZ'], quotes_enabled=False,
                                          history_symbols=['600519.SH', '000001.SZ'])

    def request(self, **changes):
        return dict(symbols=['600519.SH', '000001.SZ'], start_date='2026-10-13', end_date='2026-10-14', **changes)

    def test_cache_stale_partial_failure_and_regression(self):
        batch = parse_sina(sina_row() + sina_row('sz000001'), self.config['symbols'], NOW)
        self.store.save_quotes(batch, self.config['revision'])
        self.assertEqual(self.store.quote_view(NOW)['quotes'][0]['state'], 'fresh')
        self.assertEqual(self.store.quote_view(NOW + timedelta(minutes=2))['quotes'][0]['state'], 'stale')
        self.assertEqual(self.store.quote_view(NOW.replace(hour=18))['quotes'][0]['state'], 'session_closed')
        self.service.provider = Mock()
        self.service.provider.quotes.side_effect = ProviderError('network_policy', '代理拒绝建立连接')
        self.service.poll_quotes()
        view = self.store.quote_view(NOW)
        self.assertEqual(view['quotes'][0]['state'], 'fetch_failed')
        self.assertEqual(view['quotes'][0]['quote']['price'], 12.)
        self.assertEqual(self.service.failure_count, 1)
        earlier = parse_sina(sina_row(clock='09:59:00') + sina_row('sz000001', clock='09:59:00'), self.config['symbols'], NOW)
        self.store.save_quotes(earlier, self.config['revision'])
        self.assertIn('倒退', self.store.quote_view(NOW)['quotes'][0]['error'])
        # A response issued under an old config must not overwrite newer settings.
        self.store.configure(symbols=['600519.SH'], quotes_enabled=False)
        self.store.save_quotes(batch, self.config['revision'])
        self.assertEqual(len(self.store.quote_view(NOW)['quotes']), 1)

    def test_history_atomic_idempotent_revision_and_unit_boundary(self):
        fixture = HistoryFixture(fail='600519.SH')
        with self.assertRaises(ProviderError): download_history(self.store, self.request(), provider=fixture, clock=lambda: NOW)
        self.assertEqual(self.store.list('snapshot'), [])
        original = download_history(self.store, self.request(), provider=HistoryFixture(), clock=lambda: NOW)
        self.assertFalse(original['is_demo'])  # Simulated transport, source flag follows the production adapter contract.
        self.assertIsNone(original['rows'][0]['total_return_index'])
        self.assertFalse(original['download']['point_in_time_archive'])
        again = download_history(self.store, self.request(parent_id=original['id']), provider=HistoryFixture(), clock=lambda: NOW)
        self.assertEqual(again['id'], original['id'])
        revision = download_history(self.store, self.request(parent_id=original['id']), provider=HistoryFixture(changed=True), clock=lambda: NOW)
        self.assertNotEqual(revision['id'], original['id'])
        self.assertEqual(self.store.get('snapshot', original['id'])['rows'][1]['close'], 11.)
        self.assertEqual(revision['parent_id'], original['id'])

    def test_completed_dates_and_incremental_validation(self):
        self.assertEqual(closed_day(NOW), '2026-10-14')
        self.assertEqual(closed_day(NOW.replace(hour=17)), '2026-10-15')
        self.assertEqual(closed_day(NOW.replace(day=18, hour=17)), '2026-10-16')
        with self.assertRaisesRegex(ValueError, '已完成'): history_request(self.store, **{**self.request(), 'end_date': '2026-10-15'}, clock=NOW)
        parent = download_history(self.store, self.request(), provider=HistoryFixture(), clock=lambda: NOW)
        with self.assertRaisesRegex(ValueError, '同一股票'): history_request(self.store, **{**self.request(parent_id=parent['id']), 'symbols': ['600519.SH']}, clock=NOW)

    def test_daily_schedule_is_opt_in_once_after_close_and_persistent(self):
        self.service.clock = lambda: NOW.replace(hour=17)
        self.service.schedule_daily()
        self.assertEqual(self.jobs.jobs(), [])
        self.store.configure(symbols=['600519.SH'], quotes_enabled=False, auto_daily=True,
                             history_symbols=['600519.SH', '000001.SZ'], history_start='2026-10-13')
        self.service.clock = lambda: NOW
        self.service.schedule_daily(); self.assertEqual(self.jobs.jobs(), [])
        self.service.clock = lambda: NOW.replace(hour=17)
        self.service.schedule_daily(); self.service.schedule_daily()
        self.assertEqual(len(self.jobs.jobs()), 1)
        reopened = MarketService(self.temp.name, self.jobs, clock=self.service.clock, start_thread=False)
        reopened.schedule_daily(); reopened.close()
        self.assertEqual(len(self.jobs.jobs()), 1)
        self.assertEqual(self.jobs.jobs()[0]['kind'], 'history')

    def test_history_worker_success_failure_and_cancel(self):
        job = self.service.submit_history(**self.request())
        self.jobs.claim('owner')
        def download(store, request, checkpoint):
            return download_history(store, request, checkpoint=checkpoint, provider=HistoryFixture(), clock=lambda: NOW)
        with patch('quantify.worker.download_history', side_effect=download): execute(self.temp.name, job['id'], 'owner')
        result = self.jobs.job(job['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.value('latest_snapshot_summary')['row_count'], 4)
        self.assertEqual(self.store.value('latest_snapshot_id'), result['result']['snapshot_id'])
        failed = self.service.submit_history(**self.request())
        self.jobs.claim('owner')
        with patch('quantify.worker.download_history', side_effect=ProviderError('network_policy', '代理拒绝')):
            execute(self.temp.name, failed['id'], 'owner')
        self.assertEqual(self.jobs.job(failed['id'])['status'], 'failed')
        view = self.service.get('/api/market/state')
        self.assertEqual(view['history']['status'], 'failed')
        cancelled = self.service.submit_history(**self.request())
        self.jobs.claim('owner'); self.jobs.control(cancelled['id'], 'cancel')
        with patch('quantify.worker.download_history', side_effect=download): execute(self.temp.name, cancelled['id'], 'owner')
        self.assertEqual(self.jobs.job(cancelled['id'])['status'], 'cancelled')
        self.assertEqual(len(self.store.list('snapshot')), 1)


class PriceResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.wb = Workbench(self.temp.name); self.store = self.wb.store
        self.daily = datasets.import_snapshot(self.store, kind='daily', raw_csv=daily_csv(108, tri=False),
                   source='合成未复权日线测试；元/股，成交量股', price_basis='unadjusted', is_demo=True)
        self.factor = next(f for f in self.store.list('factor') if f['kind'] == 'momentum')

    def test_price_schema_report_and_ai_context_are_explicit(self):
        with self.assertRaisesRegex(ValueError, '含分红'): datasets.monthly_from_daily(self.store, daily_id=self.daily['id'])
        snapshot = datasets.monthly_from_daily(self.store, daily_id=self.daily['id'], return_basis='price')
        self.assertEqual(snapshot['kind'], 'monthly_price')
        self.assertNotIn('total_return_index', snapshot['rows'][0])
        self.assertNotIn('total_return_index', snapshot['units'])
        baseline = self.wb.run(snapshot_id=snapshot['id'], factor_ids=[self.factor['id']])
        self.assertEqual(baseline['status'], 'completed', baseline.get('error'))
        self.assertEqual(baseline['report']['return_basis'], 'price')
        self.assertIn('不含现金分红', baseline['report']['status'])
        self.assertIn('date,symbol,close,quality,value,available_at', baseline['report']['dataset']['raw_csv'])
        self.assertEqual(self.wb.replay(experiment_id=baseline['id'])['report'], baseline['report'])
        tools = ResearchTools(self.temp.name, baseline['id'], initial_progress(baseline), session_id='price-test')
        context = tools.call('get_research_context')
        self.assertEqual(context['data']['available_fields'], ['close'])
        self.assertEqual(context['baseline']['return_basis'], 'price')
        self.assertTrue(context['baseline']['data_warnings'])
        self.assertNotIn('raw_csv', json.dumps(context))
        candidate = tools.call('create_factor_candidate', name='价格公式', expression='rank(momentum(12,1))', rationale='合成流程验证')
        result = tools.call('run_experiment', factor_id=candidate['id'], experiment_id='price-test-1')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['return_basis'], 'price')
        with self.assertRaisesRegex(ValueError, '缺少'): tools.call('validate_factor', expression='quality')

    def test_price_data_never_silently_parsed_as_total_return(self):
        snapshot = datasets.monthly_from_daily(self.store, daily_id=self.daily['id'], return_basis='price')
        with self.assertRaisesRegex(ValueError, '表头'): parse_panel(datasets.csv_text('monthly_price', snapshot['rows']), required_fields=())
        with self.assertRaisesRegex(ValueError, '表头'): parse_panel(demo_csv(), required_fields=(), return_basis='price')
        with self.assertRaises(ValueError): datasets.import_snapshot(self.store, kind='monthly_price',
            raw_csv=datasets.csv_text('monthly_price', snapshot['rows']), source='invalid', price_basis='forward_adjusted', is_demo=True)

    def test_price_future_signal_and_holdout_isolation(self):
        snapshot = datasets.monthly_from_daily(self.store, daily_id=self.daily['id'], return_basis='price')
        baseline = self.wb.run(snapshot_id=snapshot['id'], factor_ids=[self.factor['id']])
        rows = copy.deepcopy(snapshot['rows'])
        for row in rows:
            if row['date'] >= baseline['protocol']['holdout_start']: row['close'] *= 100
        future = datasets.import_snapshot(self.store, kind='monthly_price', raw_csv=datasets.csv_text('monthly_price', rows),
                     source='合成留出扰动', price_basis='unadjusted', is_demo=True)
        rerun = self.wb.run(snapshot_id=future['id'], factor_ids=[self.factor['id']])
        self.assertEqual(rerun['report']['selected'], baseline['report']['selected'])
