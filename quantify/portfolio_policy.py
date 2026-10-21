"""Explicit portfolio admission criteria and aligned, non-overlapping NAV periods."""
from statistics import mean

from .core import integer, number

DEFAULT = dict(primary='positive_period_rate', min_improvement=0., max_return_deterioration=0.,
               max_drawdown_deterioration=0., max_worst_period_deterioration=0.,
               min_periods=2, min_observed_days=60, min_trades=1)


def policy(value=None):
    if value is not None and (not isinstance(value, dict) or set(value)-set(DEFAULT)):
        raise ValueError('组合验收政策字段无效')
    result = {**DEFAULT, **(value or {})}
    if result['primary'] not in ('positive_period_rate', 'total_return'):
        raise ValueError('组合主目标须为 positive_period_rate 或 total_return')
    for key in ('min_improvement', 'max_return_deterioration', 'max_drawdown_deterioration', 'max_worst_period_deterioration'):
        result[key] = number(result[key], key, 0, 1)
    for key, low, high in (('min_periods', 2, 600), ('min_observed_days', 2, 20000), ('min_trades', 1, 1000000)):
        result[key] = integer(result[key], key, low, high)
    return result


def summarize(account, boundaries):
    points = {p['date']: p for p in account['curve']}
    if any(d not in points for d in boundaries):
        raise ValueError('净值未覆盖固定比较周期端点')
    periods = [dict(start=a, end=b, net_return=points[b]['equity']/points[a]['equity']-1)
               for a, b in zip(boundaries, boundaries[1:])]
    returns = [p['net_return'] for p in periods]
    return dict(**account['summary'], observed_days=len(account['curve']), completed_periods=len(periods),
                positive_period_rate=sum(r>0 for r in returns)/len(returns) if returns else None,
                mean_period_return=mean(returns) if returns else None,
                worst_period_return=min(returns) if returns else None), periods


def assess(candidate, reference, objective):
    objective = policy(objective)
    primary = objective['primary']
    present = candidate.get(primary) is not None and reference.get(primary) is not None
    tail_present = candidate.get('worst_period_return') is not None and reference.get('worst_period_return') is not None
    checks = dict(
        primary_improved=present and candidate[primary]-reference[primary] > objective['min_improvement']+1e-12,
        return_guard=candidate['total_return']+1e-12 >= reference['total_return']-objective['max_return_deterioration'],
        drawdown_guard=candidate['max_drawdown']+1e-12 >= reference['max_drawdown']-objective['max_drawdown_deterioration'],
        period_tail_guard=tail_present and candidate['worst_period_return']+1e-12 >= reference['worst_period_return']-objective['max_worst_period_deterioration'],
        same_periods=candidate['completed_periods']==reference['completed_periods'],
        same_days=candidate['observed_days']==reference['observed_days'],
        period_floor=min(candidate['completed_periods'],reference['completed_periods'])>=objective['min_periods'],
        observed_day_floor=min(candidate['observed_days'],reference['observed_days'])>=objective['min_observed_days'],
        trade_floor=min(candidate['trade_count'],reference['trade_count'])>=objective['min_trades'])
    reasons = dict(primary_improved='组合主目标未严格改善', return_guard='累计收益低于允许下限',
                   drawdown_guard='最大回撤恶化超限', period_tail_guard='最差完整周期收益恶化超限',
                   same_periods='比较周期数量不同', same_days='观察日数量不同', period_floor='完整比较周期不足',
                   observed_day_floor='观察日不足', trade_floor='候选或参照实际模拟成交不足')
    return dict(accepted=all(checks.values()), objective=objective, checks=checks,
                reasons=[reasons[k] for k, passed in checks.items() if not passed])
