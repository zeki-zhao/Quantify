"""Local market configuration, read-only quote polling and queued daily history updates."""
import json
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta

from .core import integer
from .datasets import day, csv_text, _prepare_snapshot
from .market_providers import (SinaProvider, EastmoneyProvider, ProviderError, symbols_list,
                               local_now, CHINA, HISTORY_SOURCE)
from .storage import ResearchStore, canonical

DEFAULT_CONFIG = {'symbols': ['000001.SZ', '600519.SH'], 'quotes_enabled': False, 'interval_seconds': 10,
                  'auto_daily': False, 'history_symbols': [], 'history_start': '2015-01-01', 'revision': 0}


def closed_day(clock):
    """Exclude the current date until 16:30 China time; no exchange-calendar claim."""
    now = clock.astimezone(CHINA)
    stamp = now.date()
    if (now.hour, now.minute) < (16, 30):
        stamp -= timedelta(days=1)
    while stamp.weekday() >= 5:
        stamp -= timedelta(days=1)
    return stamp.isoformat()


def in_session(clock):
    clock = clock.astimezone(CHINA)
    minutes = clock.hour * 60 + clock.minute
    return clock.weekday() < 5 and (570 <= minutes <= 690 or 780 <= minutes <= 900)


class MarketStore(ResearchStore):
    def __init__(self, directory):
        super().__init__(directory)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS market_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS market_quotes (symbol TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def value(self, key, default=None):
        with closing(self.connect()) as db:
            row = db.execute('SELECT value FROM market_state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def write(db, key, value):
        db.execute('INSERT INTO market_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, canonical(value)))

    def set(self, key, value):
        with closing(self.connect()) as db, db:
            self.write(db, key, value)

    def config(self):
        return self.value('config', DEFAULT_CONFIG.copy())

    def configure(self, *, symbols, quotes_enabled, interval_seconds=10, auto_daily=False,
                  history_symbols=None, history_start='2015-01-01'):
        symbols = symbols_list(symbols)
        if history_symbols is None:
            history_symbols = []
        if not isinstance(history_symbols, list):
            raise ValueError('历史股票列表必须是数组')
        history_symbols = symbols_list(history_symbols) if history_symbols else []
        if type(quotes_enabled) is not bool or type(auto_daily) is not bool:
            raise ValueError('刷新与每日更新开关必须为布尔值')
        if auto_daily and not history_symbols:
            raise ValueError('每日更新需要指定历史股票列表')
        day(history_start)
        interval_seconds = integer(interval_seconds, '行情刷新间隔', 5, 3600)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM market_state WHERE key='config'").fetchone()
            revision = json.loads(row[0])['revision'] if row else 0
            config = dict(symbols=symbols, quotes_enabled=quotes_enabled, interval_seconds=interval_seconds,
                          auto_daily=auto_daily, history_symbols=history_symbols, history_start=history_start,
                          revision=revision + 1)
            self.write(db, 'config', config)
        return config

    def save_quotes(self, batch, config_revision):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM market_state WHERE key='config'").fetchone()
            revision = json.loads(row[0])['revision'] if row else 0
            if revision != config_revision:
                return  # Settings changed during I/O: discard the obsolete response.
            errors = dict(batch['errors'])
            for symbol, quote in batch['quotes'].items():
                previous = db.execute('SELECT value FROM market_quotes WHERE symbol=?', (symbol,)).fetchone()
                if previous and json.loads(previous[0])['quote_at'] > quote['quote_at']:
                    errors[symbol] = '行情时间倒退，保留上一次有效报价'
                    continue
                db.execute('INSERT INTO market_quotes VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET value=excluded.value',
                           (symbol, canonical(quote)))
            self.write(db, 'quote_status', {'attempted_at': batch['received_at'], 'errors': errors,
                                           'raw_sha256': batch.get('raw_sha256'), 'category': batch.get('category'),
                                           'config_revision': config_revision})

    def quote_view(self, clock):
        config, status = self.config(), self.value('quote_status', {})
        with closing(self.connect()) as db:
            cached = {row[0]: json.loads(row[1]) for row in db.execute('SELECT symbol,value FROM market_quotes')}
        results = []
        for symbol in config['symbols']:
            quote = cached.get(symbol)
            error = status.get('errors', {}).get(symbol) if status.get('config_revision') == config['revision'] else None
            age = max(0, (clock - datetime.fromisoformat(quote['quote_at'])).total_seconds()) if quote else None
            state = ('fetch_failed' if error else 'no_data' if not quote else
                     'session_closed' if not in_session(clock) else
                     'stale' if age > max(30, config['interval_seconds'] * 2) else 'fresh')
            results.append({'symbol': symbol, 'quote': quote, 'state': state, 'age_seconds': age, 'error': error})
        return {'config': config, 'quotes': results, 'last_attempt': status,
                'checked_at': clock.isoformat(), 'calendar_notice': '交易时段按中国时间与工作日估计，未核验节假日交易日历',
                'history': self.value('history_status', {}), 'latest_snapshot_id': self.value('latest_snapshot_id'),
                'latest_snapshot_summary': self.value('latest_snapshot_summary'),
                'last_auto_day': self.value('last_auto_day')}


def history_request(store, *, symbols, start_date, end_date, parent_id=None, full_refresh=False, clock=None):
    symbols = symbols_list(symbols)
    start, end = day(start_date), day(end_date)
    if end < start or (end - start).days > 366 * 20:
        raise ValueError('历史区间需按时间顺序且不超过 20 年')
    if end_date > closed_day(clock or local_now()):
        raise ValueError('只能获取已完成日线；当日数据请在中国时间 16:30 后请求')
    if type(full_refresh) is not bool:
        raise ValueError('全量复核开关必须为布尔值')
    if parent_id:
        parent = store.get('snapshot', parent_id)
        if (parent['kind'] != 'daily' or parent['source'] != HISTORY_SOURCE or parent['is_demo'] or
                parent['price_basis'] != 'unadjusted' or parent['symbols'] != symbols):
            raise ValueError('增量父快照必须是同一股票列表的东财未复权日线')
        if start_date > parent['date_range'][0] or end_date < parent['date_range'][1]:
            raise ValueError('增量范围应包含父快照的起止日期；更新不会静默截去旧记录')
    return dict(symbols=symbols, start_date=start_date, end_date=end_date, parent_id=parent_id,
                full_refresh=full_refresh)


def download_history(store, request, *, provider=None, clock=local_now, checkpoint=None):
    """Download all requested symbols before persisting one version; missing symbols fail the batch."""
    request = history_request(store, **request, clock=clock())
    provider = provider or EastmoneyProvider()
    parent = store.get('snapshot', request['parent_id']) if request['parent_id'] else None
    start = request['start_date']
    if parent and not request['full_refresh']:
        # Recheck recent unadjusted records; no rescaling of historic prices.
        start = max(start, (day(parent['date_range'][1]) - timedelta(days=7)).isoformat())
    rows, raw_hashes = [], {}
    ranges = {}
    for index, symbol in enumerate(request['symbols']):
        if checkpoint:
            checkpoint({'downloaded_symbols': index, 'total_symbols': len(request['symbols']), 'current_symbol': symbol})
        response = provider.history(symbol, start, request['end_date'])
        if not response['rows'] or any(row['symbol'] != symbol for row in response['rows']):
            raise ProviderError('response', '历史数据缺失或代码错配；整批未导入')
        rows.extend(response['rows']); raw_hashes[symbol] = response['raw_sha256']
        dates = sorted(row['date'] for row in response['rows'])
        ranges[symbol] = [dates[0], dates[-1]]
    if checkpoint:
        checkpoint({'downloaded_symbols': len(request['symbols']), 'total_symbols': len(request['symbols']), 'stage': 'validated'})
    body = _prepare_snapshot(store, kind='daily', raw_csv=csv_text('daily', rows), source=HISTORY_SOURCE,
                             price_basis='unadjusted', is_demo=False, parent_id=request['parent_id'])
    if 'id' in body:
        snapshot = body
    else:
        body['download'] = {'provider': 'eastmoney', 'requested_start': start, 'requested_end': request['end_date'],
                            'received_at': clock().isoformat(), 'response_sha256': raw_hashes,
                            'returned_ranges': ranges, 'point_in_time_archive': False}
        body['warnings'] += ['网页历史数据为本次获取的版本，不能证明当年所见版本；available_at 假设原始价格在行情日收盘可用。',
                             '没有交易日历、上市/退市全表或公司行动核验；返回区间不代表完整覆盖。']
        snapshot = store.put('snapshot', body)
    return snapshot


class MarketService:
    def __init__(self, directory, jobs, *, quote_provider=None, clock=local_now, start_thread=True):
        self.store, self.jobs = MarketStore(directory), jobs
        self.provider, self.clock = quote_provider or SinaProvider(), clock
        self.stop, self.wake, self.lock = threading.Event(), threading.Event(), threading.Lock()
        self.refresh_requested = False
        self.fetching = False
        self.last_fetch = 0.
        self.next_poll = 0.
        self.failure_count = 0
        self.thread = None
        if start_thread:
            self.thread = threading.Thread(target=self._loop, name='quantify-market', daemon=True)
            self.thread.start()

    def submit_history(self, **payload):
        request = history_request(self.store, **payload, clock=self.clock())
        job = self.jobs.create('history', {'request': request}, 600)
        self.store.set('history_status', {'job_id': job['id'], 'status': job['status']})
        return job

    def poll_quotes(self):
        self.fetching = True
        config = self.store.config()
        try:
            batch = self.provider.quotes(config['symbols'])
            self.failure_count = self.failure_count + 1 if not batch['quotes'] else 0
        except (ProviderError, ValueError) as exc:
            self.failure_count += 1
            batch = {'quotes': {}, 'errors': dict.fromkeys(config['symbols'], str(exc)),
                     'received_at': self.clock().isoformat(), 'category': getattr(exc, 'category', 'response')}
        self.store.save_quotes(batch, config['revision'])
        self.fetching = False
        self.last_fetch = time.monotonic()
        self.next_poll = self.last_fetch + min(300, config['interval_seconds'] * 2 ** min(self.failure_count, 5))

    def schedule_daily(self):
        config, now = self.store.config(), self.clock().astimezone(CHINA)
        stamp = now.date().isoformat()
        if not config['auto_daily'] or now.weekday() >= 5 or (now.hour, now.minute) < (16, 30):
            return
        if self.store.value('last_auto_day') == stamp:
            return
        if any(job['kind'] == 'history' and job['status'] in ('queued', 'running') for job in self.jobs.jobs()):
            return
        # Persist intent before submission so restart never causes an unbounded retry loop.
        self.store.set('last_auto_day', stamp)
        try:
            job = self.submit_history(symbols=config['history_symbols'], start_date=config['history_start'],
                                      end_date=closed_day(now), parent_id=self.store.value('latest_snapshot_id'))
            self.store.set('history_schedule', {'job_id': job['id'], 'day': stamp})
        except ValueError as exc:
            self.store.set('history_status', {'status': 'failed', 'error': str(exc), 'scheduled_at': now.isoformat()})

    def _loop(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    manual = self.refresh_requested
                    self.refresh_requested = False
                config = self.store.config()
                first = self.store.value('quote_status', {}).get('config_revision') != config['revision']
                if manual or config['quotes_enabled'] and (first or in_session(self.clock())) and time.monotonic() >= self.next_poll:
                    self.poll_quotes()
                self.schedule_daily()
            except Exception as exc:
                self.fetching = False
                self.next_poll = time.monotonic() + 30
                self.store.set('service_error', {'type': type(exc).__name__, 'at': self.clock().isoformat()})
            self.wake.wait(.5); self.wake.clear()

    def get(self, path):
        if path == '/api/market/state':
            result = self.store.quote_view(self.clock())
            history_job = result['history'].get('job_id')
            if history_job:
                job = self.jobs.job(history_job)
                result['history'].update(status=job['status'], error=job['error'], progress=job['progress'], result=job['result'])
            result['service_error'] = self.store.value('service_error')
            result['history_schedule'] = self.store.value('history_schedule')
            return result
        raise ValueError('行情路径不存在')

    def post(self, path, payload):
        if path == '/api/market/config':
            result = self.store.configure(**payload)
            self.wake.set()
            return result
        if path == '/api/market/refresh':
            if payload:
                raise ValueError('刷新无需参数')
            with self.lock:
                if time.monotonic() - self.last_fetch < 5 or self.refresh_requested or self.fetching:
                    raise ValueError('刷新间隔至少 5 秒，请稍后查看结果')
                self.refresh_requested = True
            self.wake.set()
            return {'status': 'requested'}
        if path == '/api/market/history':
            return self.submit_history(**payload)
        raise ValueError('行情路径不存在')

    def close(self):
        self.stop.set(); self.wake.set()
        if self.thread:
            self.thread.join(timeout=16)
            if self.thread.is_alive():
                raise RuntimeError('行情请求尚未结束')
