"""Application services shared by the UI and future model tool adapters."""
import hashlib
import platform
import uuid
from pathlib import Path

from . import datasets, factors
from .core import integer, number
from .research import demo_csv, evaluate, parse_panel
from .storage import ResearchStore
from .objectives import policy


def engine_fingerprint():
    root = Path(__file__).parent
    names = ('research.py', 'factors.py', 'core.py', 'datasets.py', 'workbench.py', 'expressions.py', 'objectives.py', 'lifecycle.py', 'worker.py', 'research_tools.py', 'lab.py', 'model_api.py', 'model_calls.py', 'portfolio.py', 'portfolio_service.py', 'diagnostics.py', 'diagnostics_service.py', 'trial_audit.py', 'increments.py', 'increment_service.py', 'portfolio_lifecycle.py', 'portfolio_policy.py', 'exposures.py', 'pipeline.py', 'forward_portfolio.py')
    hashed = hashlib.sha256()
    for name in names:
        hashed.update(name.encode())
        hashed.update((root / name).read_bytes())
    return {'sha256': hashed.hexdigest(), 'python': platform.python_version(), 'protocol': 'monthly-walk-forward-v3'}


class Workbench:
    def __init__(self, directory):
        self.store = ResearchStore(directory)
        factors.initialize_factors(self.store)

    def run(self, *, snapshot_id, factor_ids, horizon=3, top_k=2, cost_bps=20, holdout_months=12, objective=None, _experiment_id=None):
        # Persist failures after accepting a valid request shape, so unsuccessful research is not lost.
        request = dict(snapshot_id=snapshot_id, factor_ids=factor_ids, horizon=horizon,
                       top_k=top_k, cost_bps=cost_bps, holdout_months=holdout_months, objective=objective)
        engine = engine_fingerprint()
        try:
            objective = policy(objective)
            request['objective'] = objective
            if not isinstance(factor_ids, list) or not 1 <= len(factor_ids) <= 10:
                raise ValueError('需要选择 1 至 10 个因子版本')
            if len(set(factor_ids)) != len(factor_ids):
                raise ValueError('不能重复选择同一因子版本')
            horizon = integer(horizon, '持有月数', 3, 12)
            top_k = integer(top_k, '筛选数量', 1, 20)
            cost_bps = number(cost_bps, '往返成本', 0, 1000)
            holdout_months = integer(holdout_months, '封存月数', 12, 60)
            snapshot = self.store.get('snapshot', snapshot_id)
            if snapshot['kind'] not in ('monthly', 'monthly_price'):
                raise ValueError('实验需要月度研究快照；日线请先生成对应收益口径的月度快照')
            return_basis = 'price' if snapshot['kind'] == 'monthly_price' else 'total_return'
            specs = [self.store.get('factor', item) for item in factor_ids]
            raw = datasets.csv_text(snapshot['kind'], snapshot['rows'])
            dates, _ = parse_panel(raw, required_fields=factors.required_fields(specs), return_basis=return_basis)
            minimum = 68 + 2 * horizon + holdout_months
            if len(dates) < minimum:
                raise ValueError(f'共同预热、训练、持有及封存至少需要 {minimum} 个月数据，建议更长历史')
            cutoff = dates[-holdout_months].isoformat()
            # Evaluator sees only development rows, including its realized labels.
            development = [row for row in snapshot['rows'] if row['date'] < cutoff]
            development_csv = datasets.csv_text(snapshot['kind'], development)
            report = evaluate(development_csv, horizon=horizon, top_k=top_k, cost_bps=cost_bps,
                              source=snapshot['source'], factor_specs=specs, warmup_months=30, return_basis=return_basis, objective=objective)
            report['is_demo'] = snapshot['is_demo']
            report['status'] = ('合成数据，仅验证流程' if snapshot['is_demo'] else
                                '导入数据的探索性验证；来源声明未经外部核验')
            if return_basis == 'price':
                report['status'] += '；未复权价格收益，不含现金分红，公司行动跳变未调整'
            report['factor_definitions'] = {spec['id']: (spec['expression'] if spec['kind'] == 'expression' else f"{spec['name']} / {spec['kind']} / 窗口 {spec['lookback']} / 跳过 {spec['skip']}")
                                            for spec in specs}
            report['snapshot_id'] = snapshot_id
            report['snapshot_warnings'] = snapshot['warnings']
            protocol = {'deterministic': True, 'random_seed': None, 'warmup_months': 30, 'holdout_months': holdout_months, 'holdout_start': cutoff,
                        'holdout_evaluated': False, 'objective': objective, 'return_basis': return_basis, 'development_end': development[-1]['date'],
                        'note': '末尾月份封存且不参与本次计算；开发窗可反复比较，不代表独立最终验收。'}
            body = {'status': 'completed', 'request': request, 'engine': engine, 'protocol': protocol, 'report': report}
        except (ValueError, TypeError, KeyError) as exc:
            body = {'status': 'failed', 'request': request, 'engine': engine, 'error': str(exc)}
        return self.store.put('experiment', body, _experiment_id or uuid.uuid4().hex)

    def compare(self, *, left_id, right_id):
        left = self.store.get('experiment', left_id)
        right = self.store.get('experiment', right_id)
        if any(item['status'] != 'completed' for item in (left, right)):
            raise ValueError('只能比较成功完成的实验')
        for key in ('snapshot_id', 'horizon', 'top_k', 'cost_bps', 'holdout_months', 'objective'):
            if left['request'].get(key) != right['request'].get(key):
                raise ValueError('实验数据与评估参数必须相同，才能比较因子版本')
        if left['engine'] != right['engine'] or left['protocol'] != right['protocol']:
            raise ValueError('引擎或评估协议不同，请使用同一版本重新实验')
        # Defend the common-window contract even if a future engine changes eligibility.
        def windows(item):
            return [(fold['test_signal_start'], fold['test_last_exit']) for fold in item['report']['folds']]
        if windows(left) != windows(right):
            raise ValueError('因子历史窗口导致测试时段不同，不能直接对比；请使用相同历史窗口')
        metrics = ('mean_return', 'positive_rate', 'worst_return', 'observations')
        return {'left_id': left_id, 'right_id': right_id,
                'difference_right_minus_left': {key: right['report']['selected'][key] - left['report']['selected'][key]
                                               for key in metrics},
                'note': '只比较开发窗单笔样本统计，非组合净值或最终独立验收；全部历史尝试仍保留。'}

    def replay(self, *, experiment_id):
        old = self.store.get('experiment', experiment_id)
        if old['engine'] != engine_fingerprint():
            raise ValueError('引擎版本已改变；请恢复对应代码环境后复现，或作为新实验运行')
        return self.run(**old['request'])

    def get(self, path):
        if path == '/api/workspace/snapshots':
            return [datasets.snapshot_summary(item) for item in self.store.list('snapshot')]
        if path == '/api/workspace/factors':
            return self.store.list('factor')
        if path == '/api/workspace/experiments':
            return [{key: value for key, value in item.items() if key != 'report'}
                    for item in self.store.list('experiment')]
        for plural, kind in (('snapshots', 'snapshot'), ('experiments', 'experiment')):
            prefix = '/api/workspace/' + plural + '/'
            if path.startswith(prefix):
                return self.store.get(kind, path[len(prefix):])
        raise ValueError('路径不存在')

    def post(self, path, payload):
        if path == '/api/workspace/import':
            return datasets.snapshot_summary(datasets.import_snapshot(self.store, **payload))
        if path == '/api/workspace/demo':
            if payload:
                raise ValueError('演示快照无需参数')
            return datasets.snapshot_summary(datasets.import_snapshot(
                self.store, kind='monthly', raw_csv=demo_csv(), source='人工合成研究面板 v1',
                price_basis='total_return_index', is_demo=True))
        if path == '/api/workspace/monthly':
            return datasets.snapshot_summary(datasets.monthly_from_daily(self.store, **payload))
        if path == '/api/workspace/as-of':
            if set(payload) != {'snapshot_id', 'cutoff'}:
                raise ValueError('需要 snapshot_id 和 cutoff')
            return datasets.as_of_rows(self.store.get('snapshot', payload['snapshot_id']), payload['cutoff'])
        if path == '/api/workspace/factors':
            return factors.create_version(self.store, **payload)
        if path == '/api/workspace/experiments':
            return self.run(**payload)
        if path == '/api/workspace/compare':
            return self.compare(**payload)
        if path == '/api/workspace/replay':
            return self.replay(**payload)
        raise ValueError('路径不存在')
