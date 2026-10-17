"""Read-only experiment/proposal census. Repeated attempts remain visible across sessions."""
import json
from collections import Counter,defaultdict
from contextlib import closing
from datetime import datetime,timezone

from .expressions import validate_expression
from .storage import digest


def factor_signature(spec):
    kind=spec['kind']
    expression=spec.get('expression')
    if kind in ('quality','value'):expression=kind
    if kind=='momentum':expression=f"momentum({spec['lookback']},{spec['skip']})"
    if kind=='low_volatility':expression=f"-volatility({spec['lookback']},{spec['skip']})"
    return digest(dict(normalized=validate_expression(expression)['normalized'])) if expression else digest({k:spec[k] for k in ('kind','lookback','skip')})


def protocol_key(experiment):
    return digest(dict(request={k:v for k,v in experiment['request'].items() if k!='factor_ids'},engine=experiment['engine']))


def audit_trials(store,experiment_id=None):
    # Snapshot all records within one read transaction: mutable jobs must not change mid-census.
    with closing(store.connect()) as db:
        db.execute('BEGIN')
        # Keep kinds separately, since artifacts need not be content-addressed.
        rows=db.execute("SELECT kind,id,body FROM artifacts WHERE kind IN ('experiment','factor')")
        by_kind=defaultdict(dict)
        for kind,i,body in rows:
            item=json.loads(body)
            if kind=='experiment':item={key:item[key] for key in ('request','engine','status')}
            by_kind[kind][i]=dict(id=i,**item)
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        jobs=[dict(id=i,status=status,payload=json.loads(payload),progress=json.loads(progress)) for i,status,payload,progress in db.execute("SELECT id,status,payload,progress FROM jobs WHERE kind='research'")] if 'jobs' in tables else []
        events=[(i,json.loads(body)) for i,body in db.execute("SELECT job_id,body FROM job_events WHERE event='proposal' ORDER BY sequence")] if 'job_events' in tables else []
    experiments=by_kind['experiment']
    if experiment_id is not None and (not isinstance(experiment_id,str) or experiment_id not in experiments):
        raise ValueError('实验记录不存在')
    anchor=experiments[experiment_id] if experiment_id else None
    scope=protocol_key(anchor) if anchor else None
    selected={i:r for i,r in experiments.items() if scope is None or protocol_key(r)==scope}
    jobs=[j for j in jobs if j['payload'].get('baseline_id') in selected]
    proposals={};job_map={j['id']:j for j in jobs}
    for job_id,e in events:
        if job_id in job_map and type(e.get('round')) is int:proposals[(job_id,e['round'])]=dict(proposal=e.get('proposal',{}),status='unresolved')
    for job in jobs:
        p=job['progress']
        if p.get('pending'):
            item=p['pending'];proposals[(job['id'],item['round'])]=dict(proposal=item.get('proposal',{}),status='in_progress' if job['status'] in ('running','queued','paused') else 'unresolved_'+job['status'])
        for outcome in p.get('outcomes',[]):
            proposals[(job['id'],outcome['round'])]=dict(proposal=outcome.get('proposal',{}),status=outcome['status'])
    formula_groups=defaultdict(list);stops=0;invalid=0
    for (job_id,round_number),row in sorted(proposals.items()):
        proposal=row['proposal']
        if proposal.get('action')=='stop':stops+=1;continue
        if proposal.get('action')!='propose':continue
        try:signature=digest(dict(normalized=validate_expression(proposal.get('expression'))['normalized']))
        except ValueError:signature=None;invalid+=1
        if signature:formula_groups[signature].append(dict(session_id=job_id,round=round_number,status=row['status']))
    factor_groups=defaultdict(list)
    if scope is None:
        for spec in by_kind['factor'].values():factor_groups[factor_signature(spec)].append(spec['id'])
    experiment_groups=defaultdict(list)
    for exp in selected.values():
        ids=exp['request'].get('factor_ids',[])
        signatures=[factor_signature(by_kind['factor'][i]) if isinstance(i,str) and i in by_kind['factor'] else digest(i) for i in ids] if isinstance(ids,list) else [digest(ids)]
        experiment_groups[digest(dict(protocol=protocol_key(exp),factors=signatures))].append(exp['id'])
    consumed=sum(len(v) for v in formula_groups.values())+invalid
    statuses=Counter(row['status'] for row in proposals.values() if row['proposal'].get('action')=='propose')
    return dict(as_of=datetime.now(timezone.utc).isoformat(),scope='same_protocol' if scope else 'local_directory',
                protocol_key=scope,experiment_id=experiment_id,
                counts=dict(experiments=len(selected),experiment_statuses=dict(Counter(e['status'] for e in selected.values())),
                            repeated_experiments=sum(len(v)-1 for v in experiment_groups.values()),sessions=len(jobs),
                            proposals_consumed=consumed,unique_valid_formulas=len(formula_groups),invalid_formulas=invalid,
                            repeated_proposals=sum(len(v)-1 for v in formula_groups.values()),stop_proposals=stops,
                            proposal_statuses=dict(statuses),
                            submitted_unconsumed=sum(max(0,len(j['payload'].get('proposals',[]))-j['progress'].get('cursor',0)) for j in jobs)),
                repeated_formula_groups=[dict(signature=k,attempts=len(v),sessions=len({r['session_id'] for r in v}),records=v) for k,v in sorted(formula_groups.items()) if len(v)>1],
                repeated_experiment_groups=[dict(signature=k,experiment_ids=v) for k,v in sorted(experiment_groups.items()) if len(v)>1],
                repeated_factor_definitions=[dict(signature=k,factor_ids=v) for k,v in sorted(factor_groups.items()) if len(v)>1] if scope is None else [],
                note='仅同一数据目录内的可观察记录；重放/失败/重复不删除，不是独立试验数。重复仅按结构化公式/参数识别，不证明代数等价。诊断任务不计为收益实验；未消费提案不计尝试。')
