"""Immutable multi-factor references, queued paired evaluations and scoped AI summaries."""
from . import factors
from .core import integer, number
from .diagnostics_service import development_data
from .increments import analyze
from .storage import digest
from .workbench import engine_fingerprint


class IncrementService:
    def __init__(self, workbench, jobs=None):
        self.workbench, self.store, self.jobs = workbench, workbench.store, jobs

    def request(self, *, experiment_id, base_factor_id, candidate_factor_id, candidate_weight=.25):
        experiment = self.store.get('experiment', experiment_id)
        if experiment['status'] != 'completed' or experiment['engine'] != engine_fingerprint():
            raise ValueError('增量评估需要当前引擎成功完成的开发实验')
        if base_factor_id not in experiment['request']['factor_ids']:
            raise ValueError('原组合必须被源实验明确引用')
        base = self.store.get('factor', base_factor_id)
        candidate = self.store.get('factor', candidate_factor_id)
        if candidate['kind'] == 'blend':
            raise ValueError('新增候选必须是非组合因子')
        children = factors.constituents(base)
        if len(children) >= 10 or candidate_factor_id in [c['factor']['id'] for c in children]:
            raise ValueError('新增候选不能重复已有版本，加入后最多10个成分')
        return dict(experiment_id=experiment_id, base_factor_id=base_factor_id,
                    candidate_factor_id=candidate_factor_id,
                    candidate_weight=number(candidate_weight, '候选权重', .01, .99))

    def submit(self, *, replay_id=None, timeout_seconds=120, **arguments):
        if not self.jobs:
            raise ValueError('增量评估需运行中的队列服务')
        if replay_id:
            if arguments:
                raise ValueError('复现编号不能与新参数混用')
            old = self.store.get('factor_increment', replay_id)
            if old['engine'] != engine_fingerprint():
                raise ValueError('增量引擎改变，不能跨版本复现')
            arguments = old['request']
        request = self.request(**arguments)
        return self.jobs.create('increment', dict(request=request, engine=engine_fingerprint()),
                                integer(timeout_seconds, '增量评估时限', 1, 3600))

    def run(self, *, checkpoint=None, **arguments):
        request = self.request(**arguments)
        engine = engine_fingerprint()
        artifact_id = digest(dict(request=request, engine=engine))
        try:
            cached = self.store.get('factor_increment', artifact_id)
            if cached['request'] != request or cached['engine'] != engine:
                raise ValueError('增量缓存协议冲突')
            if checkpoint:
                checkpoint(dict(cached=True))
            return cached
        except ValueError as exc:
            if str(exc) != '记录不存在':
                raise
        try:
            experiment = self.store.get('experiment', request['experiment_id'])
            base = self.store.get('factor', request['base_factor_id'])
            candidate = self.store.get('factor', request['candidate_factor_id'])
            augmented = factors.augment(self.store, base, candidate, request['candidate_weight'])
            snapshot, basis, dates, panel, windows = development_data(self.store, experiment)
            report = analyze(dates=dates, panel=panel, base=base, candidate=candidate, augmented=augmented,
                             windows=windows, horizon=experiment['request']['horizon'],
                             top_k=experiment['request']['top_k'], cost_bps=experiment['request']['cost_bps'],
                             objective=experiment['request']['objective'], checkpoint=checkpoint)
            report.update(base_factor=base, candidate_factor=candidate, augmented_factor=augmented,
                          is_demo=snapshot['is_demo'], return_basis=basis, warnings=snapshot['warnings'],
                          protocol=dict(experiment['protocol'], horizon=experiment['request']['horizon'],
                                        top_k=experiment['request']['top_k'], cost_bps=experiment['request']['cost_bps'],
                                        signals='source_experiment_nonoverlapping_test_windows',
                                        first_signal=dates[windows[0][0]].isoformat(),
                                        last_exit=dates[windows[-1][0]+1+experiment['request']['horizon']].isoformat()),
                          note='所有版本使用固定开发测试窗与股票池；同分按代码升序取Top K。原组合固定，不沿用源实验逐窗择优。单笔统计不是组合净值；无行业/多重检验校正，不替代独立最终验收。')
            body = dict(request=request, engine=engine, status='completed', report=report)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            body = dict(request=request, engine=engine, status='failed', error=str(exc))
        return self.store.put('factor_increment', body, artifact_id)

    def references(self, factor_id):
        self.store.get('factor', factor_id)
        def uses(spec):
            return spec['id'] == factor_id or (spec['kind'] == 'blend' and
                                              any(c['factor']['id'] == factor_id for c in spec['components']))
        all_factors = self.store.list('factor')
        bound_ids = {s['id'] for s in all_factors if uses(s)}
        return dict(factor_id=factor_id,
                    blends=[s['id'] for s in all_factors if s['kind'] == 'blend' and uses(s) and s['id'] != factor_id],
                    derived_versions=[s['id'] for s in all_factors if s.get('parent_id') == factor_id],
                    experiments=[e['id'] for e in self.store.list('experiment')
                                 if isinstance(e['request'].get('factor_ids'), list) and
                                 bound_ids.intersection(i for i in e['request']['factor_ids'] if isinstance(i, str))],
                    portfolio_specs=[s['id'] for s in self.store.list('portfolio_spec') if s['factor_id'] in bound_ids],
                    portfolio_freezes=[f['id'] for f in self.store.list('portfolio_freeze')
                                       if f['spec']['factor_id'] in bound_ids or (f['reference_spec'] and f['reference_spec']['factor_id'] in bound_ids)],
                    freezes=[s['id'] for s in self.store.list('freeze') if s['factor_id'] in bound_ids or s.get('reference_factor_id') in bound_ids],
                    increments=[r['id'] for r in self.store.list('factor_increment') if
                                r['request']['base_factor_id'] in bound_ids or r['request']['candidate_factor_id'] in bound_ids or
                                r.get('report', {}).get('augmented_factor', {}).get('id') in bound_ids],
                    note='本地直接引用及一层多因子引用；派生版本单列，不改写历史绑定。')

    def get(self, path):
        if path == '/api/increments/reports':
            return [{k: v for k, v in r.items() if k != 'report'} for r in self.store.list('factor_increment')]
        if path.startswith('/api/increments/reports/'):
            return self.store.get('factor_increment', path.removeprefix('/api/increments/reports/'))
        if path.startswith('/api/increments/references/'):
            return self.references(path.removeprefix('/api/increments/references/'))
        raise ValueError('增量路径不存在')

    def post(self, path, payload):
        if path == '/api/increments/blends':
            return factors.create_blend(self.store, **payload)
        if path == '/api/increments/reports':
            return self.submit(**payload)
        raise ValueError('增量路径不存在')
