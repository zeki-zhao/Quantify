"""Scoped research tools: fixed baseline/data/protocol, with no holdout-row tool."""
from . import factors
from .diagnostics_service import DiagnosticService
from .trial_audit import audit_trials
from .expressions import GRAMMAR, validate_expression, evaluate_expression
from .proposals import PROPOSAL_SCHEMA
from .research import parse_panel
from .workbench import Workbench, engine_fingerprint

TOOL_NAMES = ('get_research_context', 'list_factors', 'get_factor_version', 'validate_factor',
              'create_factor_candidate', 'run_experiment', 'get_experiment_result', 'compare_runs',
              'propose_strategy_update', 'get_factor_diagnostics')


def summarize(experiment):
    result = {key: experiment[key] for key in ('id', 'status', 'request', 'engine')}
    if experiment['status'] != 'completed':
        result['error'] = experiment.get('error', '实验未完成')
        return result
    report = experiment['report']
    result.update(protocol=experiment['protocol'], is_demo=report['is_demo'],
                  selected=report['selected'], baseline=report['baseline'], folds=report['folds'],
                  data_window=report['data_window'], warning=report['status'],
                  return_basis=report.get('return_basis', 'total_return'), price_basis=report['dataset']['price_basis'], data_warnings=report.get('snapshot_warnings', []))
    return result


class ResearchTools:
    def __init__(self, directory, baseline_id, progress, recorder=None, session_id=None):
        self.workbench = Workbench(directory)
        self.store = self.workbench.store
        self.baseline = self.store.get('experiment', baseline_id)
        if self.baseline['status'] != 'completed':
            raise ValueError('研究需要成功完成的基线实验')
        if self.baseline['engine'] != engine_fingerprint():
            raise ValueError('基线属于旧引擎，请按原配置重新运行生成当前引擎基线')
        self.progress = progress
        self.recorder = recorder
        self.session_id = session_id

    def call(self, tool_name, **arguments):
        if tool_name not in TOOL_NAMES:
            raise ValueError('不支持的研究工具')
        try:
            result = getattr(self, '_' + tool_name)(**arguments)
        except (ValueError, TypeError) as exc:
            if self.recorder:
                self.recorder('tool', {'name': tool_name, 'arguments': arguments, 'status': 'rejected', 'error': str(exc)})
            raise
        if self.recorder:
            # Outputs contain only development summaries/definitions, never raw rows.
            self.recorder('tool', {'name': tool_name, 'arguments': arguments, 'status': 'completed', 'result': result})
        return result

    def _scoped_experiment(self, experiment_id):
        if experiment_id not in self.progress['experiment_ids']:
            raise ValueError('实验不属于当前研究任务')
        experiment = self.store.get('experiment', experiment_id)
        for key in ('snapshot_id', 'horizon', 'top_k', 'cost_bps', 'holdout_months', 'objective'):
            if experiment['request'][key] != self.baseline['request'][key]:
                raise ValueError('实验超出固定研究协议')
        if experiment['engine'] != self.baseline['engine']:
            raise ValueError('实验引擎与研究基线不一致')
        return experiment

    def _get_research_context(self):
        report = self.baseline['report']
        _, panel = parse_panel(report['dataset']['raw_csv'], required_fields=(), return_basis=report.get('return_basis', 'total_return'))
        return {
            'baseline': summarize(self.baseline),
            'data': {'snapshot_id': self.baseline['request']['snapshot_id'], 'is_demo': report['is_demo'],
                     'development_months': len(panel), 'symbols_per_month': len(panel[0]),
                     'available_fields': ['close' if report.get('return_basis') == 'price' else 'total_return_index'] +
                        [key for key in ('quality', 'value') if all(row[key] is not None for month in panel for row in month.values())]},
            'factors': self._list_factors(), 'formula_language': GRAMMAR, 'proposal_schema': PROPOSAL_SCHEMA,
            'objective': self.baseline['request']['objective'],
            'diagnostics': self._get_factor_diagnostics(self.progress['best_id']),
            'selection_note': '相同目标同时用于训练和候选准入；无合格替代者时保留候选列表首个基线。不是组合回测。',
            'limitations': ['只发送开发窗指标，不发送原始行情或留出期收益。',
                            '固定股票池可能有幸存者偏差，多轮候选比较不等于独立最终验证。',
                            '提案来自手动输入或固定模型API；效果仅由本地引擎检验，模型不能启用版本。'],
        }

    def _get_factor_diagnostics(self, experiment_id, factor_ids=None):
        experiment = self._scoped_experiment(experiment_id)
        if factor_ids is None:
            factor_ids = list(dict.fromkeys([self.baseline['request']['factor_ids'][0], experiment['request']['factor_ids'][0]]))
        if not isinstance(factor_ids, list) or not 1 <= len(factor_ids) <= 10:
            raise ValueError('诊断需1至10个会话内因子')
        for factor_id in factor_ids:
            self._get_factor_version(factor_id)
        service = DiagnosticService(self.workbench)
        from .diagnostics import compact_report
        artifact = service.run(experiment_id=experiment_id, factor_ids=factor_ids)
        return dict(report=compact_report(artifact), trial_counts=audit_trials(self.store, experiment_id)['counts'],
                    note='仅当前固定协议的试验计数；非独立样本量。高相关不证明组合增量。')

    def _list_factors(self):
        return [self.store.get('factor', factor_id) for factor_id in self.progress['factor_ids']]

    def _get_factor_version(self, factor_id):
        if factor_id not in self.progress['factor_ids']:
            raise ValueError('因子不属于当前任务')
        return self.store.get('factor', factor_id)

    def _validate_factor(self, expression):
        info = validate_expression(expression)
        _, panel = parse_panel(self.baseline['report']['dataset']['raw_csv'], required_fields=info['required_fields'],
                               return_basis=self.baseline['report'].get('return_basis', 'total_return'))
        evaluate_expression(expression, panel, 30)
        return info

    def _create_factor_candidate(self, *, name, expression, rationale):
        self._validate_factor(expression)
        parent_id = self._scoped_experiment(self.progress['best_id'])['request']['factor_ids'][0]
        candidate = factors.create_version(self.store, parent_id=parent_id, name=name,
                                           reason=rationale, expression=expression)
        if candidate['id'] not in self.progress['factor_ids']:
            self.progress['factor_ids'].append(candidate['id'])
        return candidate

    def _run_experiment(self, factor_id, experiment_id):
        self._get_factor_version(factor_id)
        if not self.session_id or not experiment_id.startswith(self.session_id + '-'):
            raise ValueError('实验编号必须属于当前研究任务')
        try:
            result = self.store.get('experiment', experiment_id)
        except ValueError as exc:
            if str(exc) != '记录不存在':
                raise
            request = {**self.baseline['request'], 'factor_ids': [factor_id]}
            result = self.workbench.run(**request, _experiment_id=experiment_id)
        if result['request']['factor_ids'] != [factor_id]:
            raise ValueError('已有实验编号与候选不一致')
        if experiment_id not in self.progress['experiment_ids']:
            self.progress['experiment_ids'].append(experiment_id)
        return summarize(self._scoped_experiment(experiment_id))

    def _get_experiment_result(self, experiment_id):
        return summarize(self._scoped_experiment(experiment_id))

    def _compare_runs(self, left_id, right_id):
        self._scoped_experiment(left_id)
        self._scoped_experiment(right_id)
        return self.workbench.compare(left_id=left_id, right_id=right_id)

    def _propose_strategy_update(self, experiment_id):
        result = self._get_experiment_result(experiment_id)
        body = {'session_id': self.session_id, 'baseline_id': self.baseline['id'], 'candidate_id': experiment_id,
                'status': 'research_suggestion_only', 'result': result,
                'note': '研究候选建议，不启用策略，不连接交易。仍需独立验证和人工判断。'}
        return self.store.put('research_proposal', body)
