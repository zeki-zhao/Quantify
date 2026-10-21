"""Immutable development diagnostics, shared by queued UI jobs and scoped AI tools."""
from . import datasets
from .core import integer
from .diagnostics import analyze,compact_report
from .research import parse_panel
from .storage import digest
from .trial_audit import audit_trials
from .workbench import engine_fingerprint


def development_data(store, experiment):
    """One source of truth for matured, non-overlapping development test windows."""
    snapshot = store.get('snapshot', experiment['request']['snapshot_id'])
    rows = [r for r in snapshot['rows'] if r['date'] < experiment['protocol']['holdout_start']]
    basis = 'price' if snapshot['kind'] == 'monthly_price' else 'total_return'
    dates, panel = parse_panel(datasets.csv_text(snapshot['kind'], rows), required_fields=(), return_basis=basis)
    horizon = experiment['request']['horizon']
    windows = []
    for fold_number, fold in enumerate(experiment['report']['folds'], 1):
        start = next(i for i, d in enumerate(dates) if d.isoformat() == fold['test_signal_start'])
        for i in range(start, min(start + 12, len(panel) - horizon - 1), horizon):
            if dates[i + 1 + horizon].isoformat() > fold['test_last_exit']:
                raise ValueError('诊断窗口与源实验不一致')
            windows.append((i, fold_number))
    if not windows:
        raise ValueError('没有可用的开发期成熟测试信号窗')
    return snapshot, basis, dates, panel, windows


class DiagnosticService:
    def __init__(self,workbench,jobs=None):
        self.workbench,self.store,self.jobs=workbench,workbench.store,jobs

    def request(self,*,experiment_id,factor_ids=None,group_count=3):
        experiment=self.store.get('experiment',experiment_id)
        if experiment['status']!='completed' or experiment['engine']!=engine_fingerprint():
            raise ValueError('诊断需要当前引擎成功完成的开发实验；旧实验请按原参数重跑')
        ids=experiment['request']['factor_ids'] if factor_ids is None else factor_ids
        if not isinstance(ids,list) or not 1<=len(ids)<=10 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
            raise ValueError('诊断需选择1至10个不重复的因子版本')
        for i in ids:self.store.get('factor',i)
        count=integer(group_count,'分组数',2,10)
        snapshot=self.store.get('snapshot',experiment['request']['snapshot_id'])
        if count>len(snapshot['symbols']):raise ValueError('分组数不能超过固定股票池数量')
        return dict(experiment_id=experiment_id,factor_ids=ids,group_count=count)

    def submit(self,*,experiment_id=None,factor_ids=None,group_count=3,replay_id=None,timeout_seconds=120):
        if not self.jobs:raise ValueError('诊断需运行中的队列服务')
        if replay_id:
            if experiment_id is not None or factor_ids is not None or group_count!=3:
                raise ValueError('复现编号不能与新诊断参数混用')
            old=self.store.get('diagnostic',replay_id)
            if old['engine']!=engine_fingerprint():raise ValueError('诊断引擎已经改变，不能跨版本复现')
            request=self.request(**old['request'])
        else:request=self.request(experiment_id=experiment_id,factor_ids=factor_ids,group_count=group_count)
        return self.jobs.create('diagnostic',dict(request=request,engine=engine_fingerprint()),integer(timeout_seconds,'诊断运行时限',1,3600))

    def run(self,*,experiment_id,factor_ids=None,group_count=3,checkpoint=None):
        request=self.request(experiment_id=experiment_id,factor_ids=factor_ids,group_count=group_count)
        engine=engine_fingerprint();artifact_id=digest(dict(request=request,engine=engine))
        try:
            existing=self.store.get('diagnostic',artifact_id)
            if existing['request']!=request or existing['engine']!=engine:raise ValueError('诊断缓存协议冲突')
            if checkpoint:checkpoint(dict(cached=True))
            return existing
        except ValueError as exc:
            if str(exc)!='记录不存在':raise
        experiment=self.store.get('experiment',experiment_id)
        try:
            snapshot,basis,dates,panel,windows=development_data(self.store,experiment)
            horizon=experiment['request']['horizon']
            specs=[self.store.get('factor',i) for i in request['factor_ids']]
            report=analyze(dates=dates,panel=panel,specs=specs,windows=windows,horizon=horizon,
                           cost_bps=experiment['request']['cost_bps'],group_count=request['group_count'],checkpoint=checkpoint)
            report.update(is_demo=snapshot['is_demo'],return_basis=basis,warnings=snapshot['warnings'],
                          protocol=dict(snapshot_id=snapshot['id'],holdout_start=experiment['protocol']['holdout_start'],
                                        holdout_evaluated=False,development_end=experiment['protocol']['development_end'],
                                        horizon=horizon,cost_bps=experiment['request']['cost_bps'],group_count=request['group_count'],
                                        signals='source_experiment_nonoverlapping_test_windows',
                                        first_signal=dates[windows[0][0]].isoformat(),last_exit=dates[windows[-1][0]+1+horizon].isoformat()),
                          notes=['Rank IC为截面分值与下一观察点入场后成熟净收益的Spearman相关；并列取平均秩，常数或不足3个配对为null。',
                                 '只使用源实验非重叠开发测试信号窗，每窗等权汇总IC和高低组差；窗口/股票相关，不提供显著性、ICIR或年化。',
                                 '分组按平均秩分桶，同分不按代码拆分；组数和人数可能不等或为空，空组为null，高低组差仅在两端均有样本时报告。',
                                 '分组净收益按股票×窗口等权，不是组合净值；高低组差不是可执行多空回报。输入缺失导致整截面拒算，覆盖不足不能作同样本改善证明。',
                                 '相关矩阵仅用双方均成功且非恒定的共同窗，显示有效窗数；高相关不等于重复定义或因子增量证明。',
                                 '固定池有幸存者偏差；多轮开发择优有选择偏差，仍须独立最终验证。没有行业数据，不提供行业稳定性。'])
            body=dict(request=request,engine=engine,status='completed',report=report)
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            body=dict(request=request,engine=engine,status='failed',error=str(exc))
        return self.store.put('diagnostic',body,artifact_id)

    def get(self,path):
        if path=='/api/diagnostics/reports':return [{k:v for k,v in r.items() if k!='report'} for r in self.store.list('diagnostic')]
        if path.startswith('/api/diagnostics/reports/'):return self.store.get('diagnostic',path.removeprefix('/api/diagnostics/reports/'))
        if path=='/api/diagnostics/trials':return audit_trials(self.store)
        if path.startswith('/api/diagnostics/trials/'):return audit_trials(self.store,path.removeprefix('/api/diagnostics/trials/'))
        raise ValueError('诊断路径不存在')

    def post(self,path,payload):
        if path=='/api/diagnostics/reports':return self.submit(**payload)
        raise ValueError('诊断路径不存在')

    def summary(self,experiment_id):
        return compact_report(self.run(experiment_id=experiment_id))
