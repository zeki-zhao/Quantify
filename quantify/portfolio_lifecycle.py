"""Frozen portfolio holdout replay and manual research-version changes; no execution gateway."""
import json
from contextlib import closing
from datetime import datetime, timezone

from . import datasets
from .core import integer
from .exposures import initialize, reserve
from .portfolio import simulate
from .portfolio_policy import policy, summarize, assess
from .portfolio_service import PortfolioService
from .storage import canonical
from .workbench import engine_fingerprint


def now():
    return datetime.now(timezone.utc).isoformat()


class PortfolioLifecycle:
    def __init__(self, workbench, jobs=None):
        self.workbench, self.store, self.jobs = workbench, workbench.store, jobs
        self.portfolio = PortfolioService(workbench)
        initialize(self.store)
        with closing(self.store.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS portfolio_lifecycle_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, freeze_id TEXT,
                previous_id TEXT, created_at TEXT, reason TEXT)''')

    def freeze(self, *, development_run_id, name='冻结组合研究版本', reference_spec_id=None, objective=None):
        name = datasets.text(name, '组合冻结名称', 100)
        run = self.store.get('portfolio_run', development_run_id)
        engine = engine_fingerprint()
        if run['status'] != 'completed' or run['engine'] != engine:
            raise ValueError('冻结需要当前引擎成功完成的开发组合回放')
        spec = self.store.get('portfolio_spec', run['spec_id'])
        if spec['engine'] != engine:
            raise ValueError('组合版本引擎不一致')
        if run['report']['protocol'].get('holdout_evaluated') is not False:
            raise ValueError('冻结来源必须是未读取留出的开发回放')
        reference = self.store.get('portfolio_spec', reference_spec_id) if reference_spec_id else None
        if reference:
            for key in ('monthly_id', 'daily_id', 'execution_id', 'holdout_start', 'development_end', 'start', 'engine'):
                if reference[key] != spec[key]:
                    raise ValueError('参照组合必须绑定相同数据、边界及引擎')
            for key in ('initial_cash', 'commission_bps', 'min_commission', 'stamp_tax_bps', 'transfer_fee_bps', 'slippage_bps'):
                if reference['config'][key] != spec['config'][key]:
                    raise ValueError('参照组合资金和费用必须一致')
        objective = policy(objective)
        snapshot = self.store.get('snapshot', spec['monthly_id'])
        dates = sorted({r['date'] for r in snapshot['rows']})
        anchor = dates.index(spec['start'])
        boundaries = [dates[i] for i in range(anchor, len(dates), spec['config']['rebalance_months']) if dates[i]>=spec['holdout_start']]
        if len(boundaries)-1 < objective['min_periods']:
            raise ValueError('留出期不足冻结政策要求的完整调仓周期；请使用更长的未暴露留出数据')
        return self.store.put('portfolio_freeze', dict(name=name, development_run_id=development_run_id,
                              spec_id=spec['id'], spec=spec, reference_spec=reference, objective=objective,
                              engine=engine, start=spec['holdout_start'], end=dates[-1], period_boundaries=boundaries,
                              symbols=snapshot['symbols'], is_demo=snapshot['is_demo'], research_only=True,
                              note='从现金开始的固定组合最终验收；冻结原有调仓相位。比较周期以候选调仓信号收盘为端点，尾段不完整周期另由累计收益覆盖。'))

    def submit(self, *, freeze_id, timeout_seconds=120):
        if not self.jobs:
            raise ValueError('组合验收需要运行中的任务服务')
        frozen = self.store.get('portfolio_freeze', freeze_id)
        if frozen['engine'] != engine_fingerprint():
            raise ValueError('冻结后引擎改变；请恢复对应环境或以新数据重新研究')
        return self.jobs.create('portfolio_validation', dict(freeze_id=freeze_id, engine=frozen['engine']),
                                integer(timeout_seconds, '组合验收时限', 1, 3600))

    def evaluate(self, *, freeze_id, checkpoint=None, job_id=None):
        frozen = self.store.get('portfolio_freeze', freeze_id)
        if frozen['engine'] != engine_fingerprint():
            raise ValueError('冻结后引擎改变，禁止修改冻结协议')
        try:
            cached = self.store.get('portfolio_evaluation', freeze_id)
            if checkpoint:
                checkpoint(dict(cached=True))
            return cached
        except ValueError as exc:
            if str(exc) != '记录不存在':
                raise
        if checkpoint:
            checkpoint(dict(stage='reserving', processed_days=0))
        overlaps = reserve(self.store, 'portfolio:'+freeze_id, frozen['start'], frozen['end'], frozen['symbols'])
        base = dict(freeze_id=freeze_id, independent=not overlaps, previous_exposures=overlaps,
                    engine=frozen['engine'], is_demo=frozen['is_demo'], research_only=True,
                    validation_job_id=job_id, activated=False)
        try:
            spec = frozen['spec']
            dates, panel, bars, states, factor, signals, monthly, daily, execution = self.portfolio._inputs(
                spec, window_start=frozen['start'], window_end=frozen['end'])
            result = simulate(dates=dates, panel=panel, daily=bars, states=states, factor=factor,
                              config=spec['config'], signal_dates=signals, checkpoint=checkpoint)
            reference = result['baseline']
            reference_decisions = None
            if frozen['reference_spec']:
                ref = frozen['reference_spec']
                rd, rp, rb, rs, rf, rsignals, *_ = self.portfolio._inputs(ref, window_start=frozen['start'], window_end=frozen['end'])
                ref_result = simulate(dates=rd, panel=rp, daily=rb, states=rs, factor=rf,
                                      config=ref['config'], signal_dates=rsignals, checkpoint=checkpoint)
                reference, reference_decisions = ref_result['selected'], ref_result['decisions']
            selected_metrics, selected_periods = summarize(result['selected'], frozen['period_boundaries'])
            reference_metrics, reference_periods = summarize(reference, frozen['period_boundaries'])
            decision = assess(selected_metrics, reference_metrics, frozen['objective'])
            # Every curve must stop at the last frozen monthly endpoint; no extra daily rows.
            if any(a['curve'][0]['date'] != frozen['start'] or a['curve'][-1]['date'] != frozen['end']
                   for a in (result['selected'], reference)):
                raise ValueError('最终回放起止日期与冻结边界不一致')
            report = dict(selected=result['selected'], reference=reference, pool=result['baseline'],
                          decisions=result['decisions'], reference_decisions=reference_decisions,
                          selected_metrics=selected_metrics, reference_metrics=reference_metrics,
                          periods=[dict(start=a['start'], end=a['end'], selected_return=a['net_return'], reference_return=b['net_return'])
                                   for a, b in zip(selected_periods, reference_periods)],
                          protocol=dict(start=frozen['start'], end=frozen['end'], holdout_evaluated=True,
                                        independent_final_validation=not overlaps, initial_state='cash_only',
                                        reference='fixed_portfolio' if frozen['reference_spec'] else 'equal_weight_pool',
                                        period_boundaries=frozen['period_boundaries'], objective=frozen['objective']),
                          warnings=list(dict.fromkeys(monthly['warnings']+daily['warnings']+execution['warnings'])),
                          note='周期上涨比例为完整调仓周期的模拟净值上涨比例，非单笔胜率或未来概率。终点持仓不强平。固定池、公司行动、执行和交易日历限制保留；启用只改变研究引用。')
            body = dict(base, status='completed', accepted=decision['accepted'] and not overlaps,
                        decision=decision, report=report)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            body = dict(base, status='failed', accepted=False, error=str(exc))
        if checkpoint:
            checkpoint(dict(stage='committing', processed_days=body.get('report', {}).get('selected_metrics', {}).get('observed_days', 0)))
        return self.store.put('portfolio_evaluation', body, freeze_id)

    def state(self):
        with closing(self.store.connect()) as db:
            rows = db.execute('SELECT seq,action,freeze_id,previous_id,created_at,reason FROM portfolio_lifecycle_events ORDER BY seq').fetchall()
        events = [dict(zip(('seq','action','freeze_id','previous_id','created_at','reason'), r)) for r in rows]
        return dict(active_id=events[-1]['freeze_id'] if events else None, events=events, research_only=True,
                    note='人工启用研究引用；不接入交易、不继承或迁移历史持仓，不自动生成组合前向账本。')

    def change(self, *, action, expected_active, reason, freeze_id=None):
        reason = datasets.text(reason, '操作理由', 500)
        if action not in ('activate', 'replace', 'rollback', 'retire'):
            raise ValueError('组合操作须为activate、replace、rollback或retire')
        if action == 'retire' and freeze_id is not None:
            raise ValueError('退役操作不接受目标版本')
        if action != 'retire':
            frozen = self.store.get('portfolio_freeze', freeze_id)
            result = self.store.get('portfolio_evaluation', freeze_id)
            if frozen['engine'] != engine_fingerprint():
                raise ValueError('组合版本属于旧引擎')
            if result['status'] != 'completed' or not result['accepted'] or not result['independent']:
                raise ValueError('仅通过独立组合最终验收的版本可启用')
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT freeze_id FROM portfolio_lifecycle_events ORDER BY seq DESC LIMIT 1').fetchone()
            active = last[0] if last else None
            if active != expected_active:
                raise ValueError('活跃组合已改变，请刷新后重试')
            target = freeze_id if action != 'retire' else None
            if target == active:
                raise ValueError('目标已是当前组合状态')
            if action == 'activate' and active is not None:
                raise ValueError('已有活跃组合，请使用替换操作')
            if action == 'replace' and active is None:
                raise ValueError('没有活跃组合，请先启用')
            if action == 'rollback' and not db.execute('SELECT 1 FROM portfolio_lifecycle_events WHERE freeze_id=?', (target,)).fetchone():
                raise ValueError('回滚目标必须曾经启用')
            if action != 'retire' and result.get('validation_job_id'):
                rows = db.execute("SELECT result FROM jobs WHERE kind='portfolio_validation' AND status='succeeded'").fetchall()
                if not any(json.loads(r[0] or '{}').get('portfolio_evaluation_id') == freeze_id for r in rows):
                    raise ValueError('组合验收报告所属任务尚未成功；取消/中断后的完整缓存需先显式重试确认')
            db.execute('INSERT INTO portfolio_lifecycle_events(action,freeze_id,previous_id,created_at,reason) VALUES (?,?,?,?,?)',
                       (action, target, active, now(), reason))
        return self.state()

    def get(self, path):
        if path == '/api/portfolio-validation/freezes':
            return self.store.list('portfolio_freeze')
        if path == '/api/portfolio-validation/evaluations':
            return [{k:v for k,v in r.items() if k!='report'} for r in self.store.list('portfolio_evaluation')]
        if path.startswith('/api/portfolio-validation/evaluations/'):
            return self.store.get('portfolio_evaluation', path.removeprefix('/api/portfolio-validation/evaluations/'))
        if path == '/api/portfolio-validation/state':
            return self.state()
        raise ValueError('组合验证路径不存在')

    def post(self, path, payload):
        if path == '/api/portfolio-validation/freeze':
            return self.freeze(**payload)
        if path == '/api/portfolio-validation/evaluate':
            return self.submit(**payload)
        if path == '/api/portfolio-validation/change':
            return self.change(**payload)
        raise ValueError('组合验证路径不存在')
