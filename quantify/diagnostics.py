"""Development-only cross-sectional diagnostics, without probability or NAV claims."""
import math
from statistics import mean, pstdev

from . import factors


def ranks(values):
    """One-based average ranks; ties never split by ticker."""
    ordered=sorted(range(len(values)),key=values.__getitem__)
    result=[0.]*len(values);start=0
    while start<len(ordered):
        end=start+1
        while end<len(ordered) and values[ordered[end]]==values[ordered[start]]:end+=1
        for i in ordered[start:end]:result[i]=(start+1+end)/2
        start=end
    return result


def spearman(left,right):
    if len(left)!=len(right):raise ValueError('相关样本必须配对')
    if len(left)<3:return None
    a,b=ranks(left),ranks(right);ma,mb=mean(a),mean(b)
    va=sum((v-ma)**2 for v in a);vb=sum((v-mb)**2 for v in b)
    if va==0 or vb==0:return None
    return max(-1.,min(1.,sum((x-ma)*(y-mb) for x,y in zip(a,b))/math.sqrt(va*vb)))


def summarize_windows(windows,group_count):
    valid=[w for w in windows if w['status']=='completed']
    ics=[w['rank_ic'] for w in valid if w['rank_ic'] is not None]
    spreads=[w['high_minus_low'] for w in valid if w['high_minus_low'] is not None]
    groups=[]
    for i in range(group_count):
        observations=sum(w['groups'][i]['observations'] for w in valid)
        total=sum((w['groups'][i]['mean_return'] or 0)*w['groups'][i]['observations'] for w in valid)
        groups.append(dict(group=i+1,observations=observations,mean_return=total/observations if observations else None))
    expected=sum(w['expected_symbols'] for w in windows)
    return dict(windows=len(windows),scored_windows=len(valid),ic_windows=len(ics),
                constant_or_undefined_ic=sum(w['rank_ic'] is None for w in valid),
                expected_scores=expected,computed_scores=sum(w['expected_symbols'] for w in valid),
                score_coverage=sum(w['expected_symbols'] for w in valid)/expected if expected else 0.,
                input_coverage=sum(w['available_symbols'] for w in windows)/expected if expected else 0.,
                mean_rank_ic=mean(ics) if ics else None,rank_ic_std=pstdev(ics) if len(ics)>1 else None,
                positive_ic_fraction=sum(v>0 for v in ics)/len(ics) if ics else None,
                mean_high_minus_low=mean(spreads) if spreads else None,spread_windows=len(spreads),groups=groups)


def analyze(*,dates,panel,specs,windows,horizon,cost_bps,group_count=3,checkpoint=None):
    reports={s['id']:dict(factor=s,windows=[]) for s in specs}
    vectors={s['id']:{} for s in specs}
    for step,(index,fold) in enumerate(windows):
        if checkpoint:checkpoint(dict(processed_windows=step,total_windows=len(windows)))
        symbols=sorted(panel[index]);entry=index+1;exit_index=entry+horizon
        outcomes=[factors.series_level(panel[exit_index][s])/factors.series_level(panel[entry][s])-1-cost_bps/10000 for s in symbols]
        for spec in specs:
            required=factors.required_fields([spec])
            available=sum(all(panel[index][s][f] is not None for f in required) for s in symbols)
            item=dict(signal=dates[index].isoformat(),entry=dates[entry].isoformat(),exit=dates[exit_index].isoformat(),
                      fold=fold,expected_symbols=len(symbols),available_symbols=available)
            try:
                scores=factors.calculate(panel,index,[spec]);values=[scores[s][spec['id']] for s in symbols]
                if not all(math.isfinite(v) for v in values):raise ValueError('因子含非有限分值')
                grouped=[[] for _ in range(group_count)]
                for rank,outcome in zip(ranks(values),outcomes):
                    bucket=min(group_count-1,int((rank-.5)*group_count/len(symbols)))
                    grouped[bucket].append(outcome)
                groups=[dict(group=i+1,observations=len(v),mean_return=mean(v) if v else None,
                             positive_rate=sum(r>0 for r in v)/len(v) if v else None,worst_return=min(v) if v else None) for i,v in enumerate(grouped)]
                spread=groups[-1]['mean_return']-groups[0]['mean_return'] if grouped[-1] and grouped[0] else None
                item.update(status='completed',rank_ic=spearman(values,outcomes),groups=groups,
                            high_minus_low=spread,pool_mean_return=mean(outcomes),distinct_scores=len(set(values)))
                vectors[spec['id']][index]=values
            except (ValueError,TypeError,KeyError,OverflowError) as exc:
                item.update(status='unavailable',reason=str(exc))
            reports[spec['id']]['windows'].append(item)
    for report in reports.values():
        rows=report['windows'];report['summary']=summarize_windows(rows,group_count)
        report['by_year']=[dict(year=year,**summarize_windows([w for w in rows if w['signal'][:4]==year],group_count)) for year in sorted({w['signal'][:4] for w in rows})]
        report['by_fold']=[dict(fold=fold,**summarize_windows([w for w in rows if w['fold']==fold],group_count)) for fold in sorted({w['fold'] for w in rows})]
    correlations=[]
    for i,left in enumerate(specs):
        for right in specs[i:]:
            common=sorted(set(vectors[left['id']])&set(vectors[right['id']]))
            paired=[dict(signal=dates[k].isoformat(),rank_correlation=spearman(vectors[left['id']][k],vectors[right['id']][k])) for k in common]
            valid=[w['rank_correlation'] for w in paired if w['rank_correlation'] is not None]
            correlations.append(dict(left_id=left['id'],right_id=right['id'],common_scored_windows=len(common),
                                     valid_windows=len(valid),mean_rank_correlation=mean(valid) if valid else None,windows=paired))
    if checkpoint:checkpoint(dict(processed_windows=len(windows),total_windows=len(windows)))
    return dict(factors=list(reports.values()),correlations=correlations,
                common_scored_windows=sum(all(k in vectors[s['id']] for s in specs) for k,_ in windows),
                expected_windows=len(windows))


def compact_report(artifact):
    """Explicit bounded summaries; excludes per-window returns, scores and input rows."""
    if artifact['status']!='completed':return dict(status='failed',error=artifact.get('error'))
    report=artifact['report']
    return dict(status='completed',experiment_id=artifact['request']['experiment_id'],
                protocol=report['protocol'],is_demo=report['is_demo'],return_basis=report['return_basis'],
                factors=[dict(factor_id=f['factor']['id'],summary=f['summary']) for f in report['factors']],
                correlations=[{k:v for k,v in r.items() if k!='windows'} for r in report['correlations']],
                note='开发测试信号窗诊断；组收益为单笔统计，非组合净值，无显著性或收益承诺。')
