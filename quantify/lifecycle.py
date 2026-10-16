"""Frozen single-factor validation and manually managed research versions (no orders)."""
import json
import uuid
from contextlib import closing
from datetime import datetime, timezone, timedelta

from . import datasets, factors
from .objectives import assess, policy
from .research import parse_panel, statistics
from .storage import canonical
from .workbench import engine_fingerprint


def now():
    return datetime.now(timezone.utc)


def panel_for(snapshot, specs):
    basis = 'price' if snapshot['kind'] == 'monthly_price' else 'total_return'
    return parse_panel(datasets.csv_text(snapshot['kind'], snapshot['rows']),
                       required_fields=factors.required_fields(specs), return_basis=basis)


def picked(panel, index, spec, top_k):
    symbols = sorted(panel[index])
    if spec is not None:
        scores = factors.calculate(panel, index, [spec])
        symbols.sort(key=lambda symbol: (-scores[symbol][spec['id']], symbol))
        symbols = symbols[:top_k]
    return symbols


def returns(panel, entry, exit_index, symbols, cost):
    return [factors.series_level(panel[exit_index][symbol]) / factors.series_level(panel[entry][symbol]) - 1 - cost / 10000
            for symbol in symbols]


class Lifecycle:
    def __init__(self, workbench):
        self.workbench = workbench
        self.store = workbench.store
        with closing(self.store.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS final_exposures (
                freeze_id TEXT PRIMARY KEY, start TEXT, end TEXT, symbols TEXT, created_at TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS lifecycle_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, strategy_id TEXT,
                previous_id TEXT, created_at TEXT, reason TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS forward_keys (
                strategy_id TEXT, signal_date TEXT, signal_id TEXT UNIQUE,
                PRIMARY KEY(strategy_id, signal_date))''')

    def freeze(self, *, experiment_id, baseline_id=None, factor_id=None, name='冻结研究版本'):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('版本名称需为 1 至 100 字')
        candidate = self.store.get('experiment', experiment_id)
        if candidate['status'] != 'completed' or candidate['engine'] != engine_fingerprint():
            raise ValueError('冻结需要当前引擎的成功开发实验')
        # Selection takes place entirely in development. The final set never trains or selects.
        factor_id = factor_id or candidate['report']['folds'][-1]['chosen_factor']
        if factor_id not in candidate['request']['factor_ids']:
            raise ValueError('最后开发窗无合格因子，请选择本实验中的具体因子版本再冻结')
        reference_factor = None
        if baseline_id:
            self.workbench.compare(left_id=baseline_id, right_id=experiment_id)
            reference_factor = self.store.get('experiment', baseline_id)['report']['folds'][-1]['chosen_factor']
        snapshot = self.store.get('snapshot', candidate['request']['snapshot_id'])
        body = dict(name=name.strip(), experiment_id=experiment_id, baseline_id=baseline_id,
                    factor_id=factor_id, reference_factor_id=reference_factor,
                    request=candidate['request'], engine=candidate['engine'],
                    objective=policy(candidate['request']['objective']),
                    holdout_start=candidate['protocol']['holdout_start'], data_end=snapshot['date_range'][1],
                    is_demo=snapshot['is_demo'], price_basis=snapshot['price_basis'],
                    minimum_signal_windows=2,
                    note='固定单因子最终验收；参照为冻结基线的最后开发窗因子，未指定基线时为全池。非组合净值。')
        return self.store.put('freeze', body)

    def evaluate(self, *, freeze_id):
        frozen = self.store.get('freeze', freeze_id)
        try:
            return self.store.get('final_evaluation', freeze_id)
        except ValueError:
            pass
        if frozen['engine'] != engine_fingerprint():
            raise ValueError('冻结后引擎改变，请恢复对应环境；不能改变冻结协议')
        snapshot = self.store.get('snapshot', frozen['request']['snapshot_id'])
        spec = self.store.get('factor', frozen['factor_id'])
        reference = self.store.get('factor', frozen['reference_factor_id']) if frozen['reference_factor_id'] else None
        symbols = sorted({row['symbol'] for row in snapshot['rows']})
        # Reserve exposure BEFORE touching holdout prices; even failed/crashed attempts consume it.
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT freeze_id,start,end,symbols FROM final_exposures').fetchall()
            if any(row[0] == freeze_id for row in previous):
                raise ValueError('此冻结版本已开始验收；结果未提交时禁止重新获得独立验收资格')
            overlaps = [row[0] for row in previous if row[1] <= frozen['data_end'] and row[2] >= frozen['holdout_start']
                        and set(json.loads(row[3])) & set(symbols)]
            db.execute('INSERT INTO final_exposures VALUES (?,?,?,?,?)',
                       (freeze_id, frozen['holdout_start'], frozen['data_end'], canonical(symbols), now().isoformat()))
        base = dict(freeze_id=freeze_id, independent=not overlaps, previous_exposures=overlaps,
                    is_demo=snapshot['is_demo'], price_basis=snapshot['price_basis'],
                    return_basis='price' if snapshot['kind'] == 'monthly_price' else 'total_return',
                    snapshot_warnings=snapshot['warnings'], engine=frozen['engine'], activated=False)
        try:
            dates, panel = panel_for(snapshot, [spec] + ([reference] if reference else []))
            horizon, top_k, cost = (frozen['request'][key] for key in ('horizon', 'top_k', 'cost_bps'))
            start = next(i for i, stamp in enumerate(dates) if stamp.isoformat() >= frozen['holdout_start'])
            selected, benchmark, pool, windows = [], [], [], []
            for index in range(start, len(panel) - horizon - 1, horizon):
                selected.extend(returns(panel, index + 1, index + 1 + horizon, picked(panel, index, spec, top_k), cost))
                benchmark.extend(returns(panel, index + 1, index + 1 + horizon, picked(panel, index, reference, top_k), cost))
                pool.extend(returns(panel, index + 1, index + 1 + horizon, sorted(panel[index]), cost))
                windows.append({'signal': dates[index].isoformat(), 'entry': dates[index+1].isoformat(),
                                'exit': dates[index+1+horizon].isoformat(), 'factor_id': spec['id']})
            if len(windows) < frozen['minimum_signal_windows']:
                raise ValueError('留出期不足两个非重叠成熟信号窗口；请用更长的新留出数据')
            selected, benchmark = statistics(selected), statistics(benchmark)
            decision = assess(selected, benchmark, frozen['objective'])
            body = {**base, 'status': 'completed', 'selected': selected, 'reference': benchmark,
                    'pool': statistics(pool), 'windows': windows, 'decision': decision,
                    'accepted': decision['accepted'] and not overlaps,
                    'note': '独立性只指本地验收台账未重用相交股票/时间；无法识别人工外部窥视。合成数据仅验证流程。'
                            if not overlaps else '该留出区间已经暴露；本次仅探索性重测，不可用于独立验收或启用。'}
        except (ValueError, TypeError, KeyError) as exc:
            body = {**base, 'status': 'failed', 'accepted': False, 'error': str(exc)}
        return self.store.put('final_evaluation', body, freeze_id)

    def events(self):
        with closing(self.store.connect()) as db:
            rows = db.execute('SELECT seq,action,strategy_id,previous_id,created_at,reason FROM lifecycle_events ORDER BY seq').fetchall()
        return [dict(zip(('seq', 'action', 'strategy_id', 'previous_id', 'created_at', 'reason'), row)) for row in rows]

    def state(self):
        events = self.events()
        active_id = events[-1]['strategy_id'] if events else None
        return {'active_id': active_id, 'events': events, 'research_only': True}

    def change(self, *, action, expected_active, reason, freeze_id=None):
        if action not in ('activate', 'rollback', 'retire') or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError('操作或理由无效')
        strategy = None
        if action != 'retire':
            frozen = self.store.get('freeze', freeze_id)
            result = self.store.get('final_evaluation', freeze_id)
            if result['status'] != 'completed' or not result['accepted'] or not result['independent']:
                raise ValueError('仅通过独立最终验收的研究版本可启用')
            if frozen['engine'] != engine_fingerprint():
                raise ValueError('研究版本属于旧引擎')
            strategy = {'freeze_id': freeze_id, 'factor_id': frozen['factor_id'],
                        'name': frozen['name'], 'request': frozen['request'], 'research_only': True}
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT strategy_id FROM lifecycle_events ORDER BY seq DESC LIMIT 1').fetchone()
            active = last[0] if last else None
            if active != expected_active:
                raise ValueError('活跃版本已改变，请刷新后重试')
            target = freeze_id if strategy else None
            if target == active:
                raise ValueError('目标已是当前状态')
            if action == 'rollback' and not db.execute('SELECT 1 FROM lifecycle_events WHERE strategy_id=?', (target,)).fetchone():
                raise ValueError('回滚目标必须曾经启用')
            if strategy:
                db.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?)',
                           ('strategy', target, now().isoformat(), canonical(strategy)))
                if db.execute('SELECT body FROM artifacts WHERE kind=? AND id=?', ('strategy', target)).fetchone()[0] != canonical(strategy):
                    raise ValueError('不可覆盖已有研究版本')
            db.execute('INSERT INTO lifecycle_events(action,strategy_id,previous_id,created_at,reason) VALUES (?,?,?,?,?)',
                       (action, target, active, now().isoformat(), reason.strip()))
        return self.state()

    def _compatible(self, snapshot, previous):
        if snapshot['kind'] != previous['kind'] or snapshot['is_demo'] != previous['is_demo'] or snapshot['price_basis'] != previous['price_basis']:
            raise ValueError('新快照的数据性质或收益口径改变')
        old = {(row['date'], row['symbol']): row for row in previous['rows']}
        new = {(row['date'], row['symbol']): row for row in snapshot['rows']}
        if any(new.get(key) != value for key, value in old.items()):
            raise ValueError('历史数据缺失或被修订，不能静默接入前向观察；请先核对并建立新版本')

    def signal(self, *, snapshot_id):
        state = self.state()
        if not state['active_id']:
            raise ValueError('请先人工启用研究版本')
        strategy_id = state['active_id']
        frozen = self.store.get('freeze', strategy_id)
        if frozen['engine'] != engine_fingerprint():
            raise ValueError('引擎改变，暂停前向生成；请恢复冻结环境或重新验证')
        snapshot = self.store.get('snapshot', snapshot_id)
        previous = self.store.get('snapshot', frozen['request']['snapshot_id'])
        self._compatible(snapshot, previous)
        spec = self.store.get('factor', frozen['factor_id'])
        dates, panel = panel_for(snapshot, [spec])
        stamp, clock = dates[-1], now()
        if clock < datetime.fromisoformat(state['events'][-1]['created_at']):
            raise ValueError('当前时间早于版本启用时间')
        today = clock.astimezone(timezone(timedelta(hours=8))).date()
        if stamp.isoformat() <= frozen['data_end'] or not timedelta(0) <= today - stamp <= timedelta(days=45):
            raise ValueError('仅对冻结后新增、日期不在未来且距当前不超过45天的最新截面生成信号')
        symbols = picked(panel, len(panel)-1, spec, frozen['request']['top_k'])
        if set(panel[-1]) != {row['symbol'] for row in previous['rows']}:
            raise ValueError('股票池发生变化，当前固定池研究不支持')
        # Serialize against replacement: no signal can be committed for a just-retired version.
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT strategy_id FROM lifecycle_events ORDER BY seq DESC LIMIT 1').fetchone()[0]
            if active != strategy_id:
                raise ValueError('活跃版本已改变')
            row = db.execute('SELECT signal_id FROM forward_keys WHERE strategy_id=? AND signal_date=?', (strategy_id, stamp.isoformat())).fetchone()
            if row:
                signal_id = row[0]
            else:
                signal_id = uuid.uuid4().hex
                body = dict(strategy_id=strategy_id, snapshot_id=snapshot_id, signal_date=stamp.isoformat(),
                            generated_at=clock.isoformat(), symbols=symbols, pool=sorted(panel[-1]),
                            factor_id=spec['id'], request=frozen['request'], is_demo=snapshot['is_demo'],
                            note='研究信号，无订单；下一月观察点入场，入场日期必须严格晚于实际生成日期。')
                db.execute('INSERT INTO artifacts VALUES (?,?,?,?)', ('forward_signal', signal_id, clock.isoformat(), canonical(body)))
                db.execute('INSERT INTO forward_keys VALUES (?,?,?)', (strategy_id, stamp.isoformat(), signal_id))
        return self.store.get('forward_signal', signal_id)

    def observe(self, *, strategy_id, snapshot_id):
        frozen = self.store.get('freeze', strategy_id)
        self.store.get('strategy', strategy_id)
        if frozen['engine'] != engine_fingerprint():
            raise ValueError('引擎改变，不能更新冻结版本的观察结果')
        snapshot = self.store.get('snapshot', snapshot_id)
        self._compatible(snapshot, self.store.get('snapshot', frozen['request']['snapshot_id']))
        dates, panel = panel_for(snapshot, [])
        horizon, cost = frozen['request']['horizon'], frozen['request']['cost_bps']
        signals = [item for item in self.store.list('forward_signal') if item['strategy_id'] == strategy_id]
        today = now().astimezone(timezone(timedelta(hours=8))).date()
        for signal in signals:
            self._compatible(snapshot, self.store.get('snapshot', signal['snapshot_id']))
        for signal in signals:
            try:
                self.store.get('forward_label', signal['id']); continue
            except ValueError:
                pass
            index = next(i for i, stamp in enumerate(dates) if stamp.isoformat() == signal['signal_date'])
            exit_index = index + 1 + horizon
            if exit_index >= len(dates) or dates[exit_index] > today:
                continue
            generated = datetime.fromisoformat(signal['generated_at']).astimezone(timezone(timedelta(hours=8))).date()
            if dates[index+1] <= generated:
                raise ValueError('入场观察日在信号生成之前或同日，拒绝事后前向标签')
            body = dict(signal_id=signal['id'], strategy_id=strategy_id, snapshot_id=snapshot_id,
                        entry=dates[index+1].isoformat(), exit=dates[exit_index].isoformat(),
                        selected_returns=returns(panel, index+1, exit_index, signal['symbols'], cost),
                        pool_returns=returns(panel, index+1, exit_index, signal['pool'], cost))
            self.store.put('forward_label', body, signal['id'])
        return self.forward_report(strategy_id)

    def forward_report(self, strategy_id):
        self.store.get('strategy', strategy_id)
        frozen = self.store.get('freeze', strategy_id)
        labels = [item for item in self.store.list('forward_label') if item['strategy_id'] == strategy_id]
        # Keep overlapping observations for traceability, but report a non-overlapping subset.
        eligible, last_exit = [], ''
        for label in sorted(labels, key=lambda item: (item['entry'], item['id'])):
            if label['entry'] >= last_exit:
                eligible.append(label); last_exit = label['exit']
        signals = [s for s in self.store.list('forward_signal') if s['strategy_id'] == strategy_id]
        result = dict(strategy_id=strategy_id, signals=signals, labels=labels,
                      evaluated_windows=len(eligible), pending_labels=len(signals)-len(labels),
                      is_demo=frozen['is_demo'], price_basis=frozen['price_basis'],
                      return_basis='price' if frozen['price_basis'] == 'unadjusted' else 'total_return',
                      snapshot_warnings=self.store.get('snapshot', frozen['request']['snapshot_id'])['warnings'],
                      state='insufficient_mature_labels', degradation=False,
                      note='单笔前向观察；非组合净值。只汇总非重叠入场区间；下降提示不构成自动替换。')
        if eligible:
            selected = statistics([value for item in eligible for value in item['selected_returns']])
            pool = statistics([value for item in eligible for value in item['pool_returns']])
            reference = self.store.get('final_evaluation', strategy_id)['selected']
            objective = frozen['objective']
            enough = len(eligible) >= 2 and selected['observations'] >= objective['min_observations']
            degradation = (selected[objective['primary']] < reference[objective['primary']] - 1e-12 or
                           selected['mean_return'] < reference['mean_return'] - objective['max_mean_deterioration'] - 1e-12 or
                           selected['worst_return'] < reference['worst_return'] - objective['max_tail_deterioration'] - 1e-12)
            result.update(selected=selected, pool=pool, final_reference=reference,
                          state='observing' if enough else 'insufficient_mature_labels',
                          degradation=enough and degradation)
        return result

    def get(self, path):
        if path == '/api/strategies/state':
            return self.state()
        if path == '/api/strategies/versions':
            return {'freezes': self.store.list('freeze'), 'evaluations': self.store.list('final_evaluation'),
                    'strategies': self.store.list('strategy')}
        if path.startswith('/api/strategies/forward/'):
            return self.forward_report(path.removeprefix('/api/strategies/forward/'))
        raise ValueError('策略路径不存在')

    def post(self, path, payload):
        names = {'freeze': self.freeze, 'evaluate': self.evaluate, 'change': self.change,
                 'signal': self.signal, 'observe': self.observe}
        name = path.removeprefix('/api/strategies/')
        if name not in names:
            raise ValueError('策略路径不存在')
        return names[name](**payload)
