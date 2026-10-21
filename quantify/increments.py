"""Paired fixed-rule factor additions and drop-one attribution on development windows."""
import math
from statistics import mean

from . import factors
from .objectives import assess
from .research import statistics

METRICS = ('mean_return', 'positive_rate', 'worst_return', 'observations')


def delta(right, left):
    return {key: right[key] - left[key] for key in METRICS} if right and left else None


def analyze(*, dates, panel, base, candidate, augmented, windows, horizon, top_k, cost_bps,
            objective, checkpoint=None):
    # Each ablation renormalizes remaining weights; no model fitting or future labels.
    variants = [('base', base), ('candidate', candidate), ('augmented', augmented)]
    for component in augmented['components']:
        omitted = component['factor']['id']
        remaining = [c for c in augmented['components'] if c['factor']['id'] != omitted]
        spec = remaining[0]['factor'] if len(remaining) == 1 else dict(
            id='without-' + omitted, kind='blend', components=remaining)
        variants.append(('without-' + omitted, spec))
    records = []
    for step, (index, fold) in enumerate(windows):
        if checkpoint:
            checkpoint(dict(processed_windows=step, total_windows=len(windows)))
        row = dict(signal=dates[index].isoformat(), entry=dates[index+1].isoformat(),
                   exit=dates[index+1+horizon].isoformat(), fold=fold)
        try:
            calculated = {}
            for key, spec in variants:
                scores = factors.calculate(panel, index, [spec])
                if any(not math.isfinite(s[spec['id']]) for s in scores.values()):
                    raise ValueError('非有限因子分值')
                symbols = sorted(scores, key=lambda s: (-scores[s][spec['id']], s))[:top_k]
                outcomes = [factors.series_level(panel[index+1+horizon][s]) /
                            factors.series_level(panel[index+1][s]) - 1 - cost_bps/10000 for s in symbols]
                calculated[key] = dict(symbols=symbols, returns=outcomes, metrics=statistics(outcomes))
            row.update(status='completed', variants=calculated,
                       augmented_minus_base=delta(calculated['augmented']['metrics'], calculated['base']['metrics']))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            # Reject the entire comparison window; never change one variant's universe alone.
            row.update(status='unavailable', reason=str(exc))
        records.append(row)

    def summary(rows):
        valid = [w for w in rows if w['status'] == 'completed']
        metrics = {key: statistics([r for w in valid for r in w['variants'][key]['returns']]) if valid else None
                   for key, _ in variants}
        difference = delta(metrics['augmented'], metrics['base'])
        complete = bool(rows) and len(valid) == len(rows)
        decision = assess(metrics['augmented'], metrics['base'], objective) if complete else dict(
            accepted=False, reasons=['比较窗口覆盖不足，不能判为改善'])
        return dict(expected_windows=len(rows), paired_windows=len(valid),
                    comparison_eligible=complete, metrics=metrics, augmented_minus_base=difference,
                    decision=decision,
                    selected_overlap=mean(len(set(w['variants']['base']['symbols']) &
                                              set(w['variants']['augmented']['symbols']))/top_k for w in valid) if valid else None,
                    positive_mean_delta_windows=sum(w['augmented_minus_base']['mean_return'] > 1e-12 for w in valid),
                    ablations=[dict(omitted_factor_id=c['factor']['id'],
                                    augmented_minus_without=delta(metrics['augmented'], metrics['without-'+c['factor']['id']]))
                               for c in augmented['components']])

    result = dict(summary=summary(records), windows=records,
                  by_year=[dict(year=y, **summary([w for w in records if w['signal'][:4] == y]))
                           for y in sorted({w['signal'][:4] for w in records})],
                  by_fold=[dict(fold=f, **summary([w for w in records if w['fold'] == f]))
                           for f in sorted({w['fold'] for w in records})])
    if checkpoint:
        checkpoint(dict(processed_windows=len(windows), total_windows=len(windows)))
    return result


def compact_report(artifact):
    """Bounded aggregate-only representation for manual and remote AI feedback."""
    if artifact['status'] != 'completed':
        return dict(status='failed', error=artifact.get('error', '增量评估未完成'))
    report = artifact['report']
    return dict(status='completed', report_id=artifact['id'], request=artifact['request'],
                augmented_factor_id=report['augmented_factor']['id'], summary=report['summary'],
                is_demo=report['is_demo'], return_basis=report['return_basis'],
                note='固定开发窗单笔统计；移除后权重重新归一，贡献不可相加；非净值、独立验收或因果证明。')
