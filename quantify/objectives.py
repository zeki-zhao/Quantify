"""One explicit policy for training, candidate admission and final validation."""
from .core import integer, number

DEFAULT = {'primary': 'positive_rate', 'min_improvement': 0.,
           'max_mean_deterioration': 0., 'max_tail_deterioration': 0., 'min_observations': 1}


def policy(value=None):
    if value is not None and (not isinstance(value, dict) or set(value) - set(DEFAULT)):
        raise ValueError('目标政策字段无效')
    result = {**DEFAULT, **(value or {})}
    if result['primary'] not in ('positive_rate', 'mean_return'):
        raise ValueError('主目标必须为 positive_rate 或 mean_return')
    for key in ('min_improvement', 'max_mean_deterioration', 'max_tail_deterioration'):
        result[key] = number(result[key], key, 0, 1)
    result['min_observations'] = integer(result['min_observations'], '最少单笔样本数', 1, 100000)
    return result


def assess(candidate, reference, objective=None):
    objective = policy(objective)
    primary = objective['primary']
    checks = {
        'primary_improved': candidate[primary] - reference[primary] > objective['min_improvement'] + 1e-12,
        'mean_guard': candidate['mean_return'] + 1e-12 >= reference['mean_return'] - objective['max_mean_deterioration'],
        'tail_guard': candidate['worst_return'] + 1e-12 >= reference['worst_return'] - objective['max_tail_deterioration'],
        'sample_floor': candidate['observations'] >= objective['min_observations'],
    }
    reasons = {'primary_improved': '主目标未超过最低改善幅度', 'mean_guard': '平均收益低于允许下限',
               'tail_guard': '最差单笔收益低于允许下限', 'sample_floor': '样本量不足'}
    return {'accepted': all(checks.values()), 'objective': objective, 'checks': checks,
            'reasons': [reasons[key] for key, passed in checks.items() if not passed]}
