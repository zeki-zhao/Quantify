"""Queue worker entry point. No arbitrary code or remote model calls are executed."""
import sys
from contextlib import closing
from .market import MarketStore, download_history
from .datasets import snapshot_summary

from .jobs import JobStore, LostLease
from .proposals import ManualProposalProvider
from .research_tools import ResearchTools
from .workbench import Workbench, engine_fingerprint


class StopRequested(Exception):
    pass


def run_research(store, job, owner):
    job_id, config, progress = job['id'], job['payload'], job['progress']
    def checkpoint():
        store.checkpoint(job_id, owner, progress)
    def record(event, body):
        store.event(job_id, event, body, owner=owner)
    def check_stop():
        current = store.job(job_id)
        if current['status'] != 'running' or current['owner'] != owner:
            raise LostLease('执行所有权已改变')
        if current['stop_requested']:
            raise StopRequested()

    tools = ResearchTools(store.directory, config['baseline_id'], progress, record, job_id)
    context = tools.call('get_research_context')
    provider = ManualProposalProvider(config['proposals'][progress['cursor']:])
    baseline = tools.baseline['report']['selected']

    def conclude(reason):
        suggestion = None
        if progress['best_id'] != config['baseline_id']:
            suggestion = tools.call('propose_strategy_update', experiment_id=progress['best_id'])['id']
        store.finish(job_id, owner, 'succeeded', result={'reason': reason, 'best_experiment_id': progress['best_id'],
                                                       'suggestion_id': suggestion, 'activated': False})

    while True:
        check_stop()
        if progress['pending'] is None:
            if progress['rounds'] >= config['max_rounds'] or progress['candidates'] >= config['max_candidates']:
                conclude('budget_exhausted'); return
            if progress['no_improvement'] >= config['patience']:
                conclude('no_improvement'); return
            proposal = provider.propose(context, progress['outcomes'])
            if proposal is None:
                store.finish(job_id, owner, 'waiting_input', result={'reason': 'awaiting_manual_proposal'})
                return
            progress['cursor'] += 1
            progress['rounds'] += 1
            progress['pending'] = {'proposal': proposal, 'round': progress['rounds'], 'stage': 'new',
                                   'experiment_id': f"{job_id}-round-{progress['rounds']}"}
            checkpoint()
            record('proposal', {'round': progress['rounds'], 'provider': 'manual', 'proposal': proposal})
        pending = progress['pending']
        proposal = pending['proposal']
        if proposal['action'] == 'stop':
            progress['outcomes'].append({'round': pending['round'], 'status': 'stopped_by_proposal', 'proposal': proposal})
            progress['pending'] = None; checkpoint()
            conclude('proposal_stop'); return
        outcome = {'round': pending['round'], 'proposal': proposal}
        try:
            if pending['stage'] == 'new':
                info = tools.call('validate_factor', expression=proposal['expression'])
                if info['normalized'] in progress['seen']:
                    raise ValueError('公式与本任务之前的候选重复')
                candidate = tools.call('create_factor_candidate', name=proposal['name'], expression=proposal['expression'],
                                       rationale=proposal['rationale'])
                pending.update(stage='validated', factor_id=candidate['id'], normalized=info['normalized'])
                progress['candidates'] += 1
                checkpoint()
            check_stop()
            result = tools.call('run_experiment', factor_id=pending['factor_id'], experiment_id=pending['experiment_id'])
            checkpoint()
            check_stop()
            outcome.update(factor_id=pending['factor_id'], experiment_id=result['id'])
            if result['status'] != 'completed':
                outcome.update(status='failed', error=result.get('error', '实验失败'))
                progress['no_improvement'] += 1
            else:
                comparison = tools.call('compare_runs', left_id=progress['best_id'], right_id=result['id'])
                delta = comparison['difference_right_minus_left']
                improved = (delta['mean_return'] > config['min_mean_improvement'] + 1e-12 and
                            result['selected']['worst_return'] >= baseline['worst_return'] - config['max_tail_deterioration'] and
                            delta['observations'] == 0)
                outcome.update(status='improved' if improved else 'not_improved', selected=result['selected'], comparison=comparison)
                if improved:
                    progress['best_id'] = result['id']
                    progress['no_improvement'] = 0
                else:
                    progress['no_improvement'] += 1
            if pending.get('normalized'):
                progress['seen'].append(pending['normalized'])
        except (ValueError, TypeError) as exc:
            outcome.update(status='rejected', error=str(exc))
            progress['no_improvement'] += 1
        progress['outcomes'].append(outcome)
        progress['pending'] = None
        checkpoint()
        record('round_completed', outcome)


def execute(directory, job_id, owner):
    store = JobStore(directory)
    job = store.job(job_id)
    if job['status'] != 'running' or job['owner'] != owner:
        return
    try:
        if job['kind'] == 'research':
            run_research(store, job, owner)
        elif job['kind'] == 'history':
            market = MarketStore(directory)
            def checkpoint(progress):
                current = store.job(job_id)
                if current['stop_requested']:
                    raise StopRequested()
                store.checkpoint(job_id, owner, progress)
            snapshot = download_history(store, job['payload']['request'], checkpoint=checkpoint)
            with closing(store.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                store._lease(db, job_id, owner)
                if db.execute('SELECT stop_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]:
                    raise StopRequested()
                market.write(db, 'latest_snapshot_id', snapshot['id'])
                market.write(db, 'latest_snapshot_summary', snapshot_summary(snapshot))
                market.write(db, 'history_status', {'job_id': job_id, 'status': 'succeeded', 'snapshot_id': snapshot['id']})
            store.finish(job_id, owner, 'succeeded', result={'snapshot_id': snapshot['id']})
        else:
            if job['payload']['engine'] != engine_fingerprint():
                raise ValueError('排队后计算代码发生变化，请重新提交任务')
            result = Workbench(directory).run(**job['payload']['request'], _experiment_id=job_id + '-experiment')
            status = 'succeeded' if result['status'] == 'completed' else 'failed'
            store.finish(job_id, owner, status, result={'experiment_id': result['id']}, error=result.get('error'))
    except StopRequested:
        store.finish(job_id, owner, 'paused')  # finish honors the actual pause/cancel request.
    except LostLease:
        return
    except (ValueError, TypeError) as exc:
        try:
            store.finish(job_id, owner, 'failed', error=str(exc))
        except LostLease:
            pass
    except Exception as exc:
        # Do not echo arbitrary process/credential contents to the browser.
        try:
            store.finish(job_id, owner, 'failed', error=f'计算进程错误：{type(exc).__name__}')
        except LostLease:
            pass


if __name__ == '__main__':
    execute(*sys.argv[1:])
