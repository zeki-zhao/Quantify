"""Durable model-call reservations; uncertain attempts never silently repeat."""
import json
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal

from .model_api import ModelError, ModelHTTP, compact_context, credential, decode_response, request_body, request_without_live_counts
from .storage import canonical, digest


def estimated_cost(config, input_tokens, output_tokens):
    return (Decimal(str(config['input_price_per_million'])) * input_tokens +
            Decimal(str(config['output_price_per_million'])) * output_tokens) / 1000000


class ModelCalls:
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS model_calls (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, round INTEGER NOT NULL,
                attempt INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                body TEXT NOT NULL, UNIQUE(job_id,round,attempt))''')

    @staticmethod
    def _rows(db, job_id):
        rows = db.execute('SELECT id,round,attempt,status,created_at,body FROM model_calls WHERE job_id=? ORDER BY round,attempt',
                          (job_id,)).fetchall()
        return [dict(id=row[0], round=row[1], attempt=row[2], status=row[3], created_at=row[4], **json.loads(row[5])) for row in rows]

    def records(self, job_id):
        with closing(self.store.connect()) as db:
            return self._rows(db, job_id)

    def summary(self, job_id):
        rows = self.records(job_id)
        return dict(calls=len(rows), charged_tokens=sum(row['charged_tokens'] for row in rows),
                    estimated_cost=float(sum((Decimal(row['charged_cost']) for row in rows), Decimal(0))),
                    reported_input_tokens=sum(row.get('input_tokens', 0) for row in rows),
                    reported_output_tokens=sum(row.get('output_tokens', 0) for row in rows),
                    uncertain_calls=sum(row['status'] in ('reserved', 'unknown') or row.get('usage_unknown', False) for row in rows),
                    currency=rows[0]['config']['currency'] if rows else None,
                    note='含结果未知调用的保守预留；费用按用户声明单价估算，不是供应商账单硬限额。')

    def reserve(self, job_id, owner, round_number, config, request):
        limits = config['limits']
        # No tokenizer dependency: conservative local estimate; actual usage is checked on settlement.
        input_bound = len(canonical(request).encode('utf-8')) + 4096
        output_bound = limits['max_output_tokens']
        tokens = input_bound + output_bound
        cost = estimated_cost(config, input_bound, output_bound)
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.store._lease(db, job_id, owner)
            if db.execute('SELECT stop_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]:
                raise ModelError('control', '已接受停止请求，未发起模型调用')
            rows = self._rows(db, job_id)
            if (len(rows) >= limits['max_calls'] or sum(row['charged_tokens'] for row in rows) + tokens > limits['max_total_tokens'] or
                    sum((Decimal(row['charged_cost']) for row in rows), Decimal(0)) + cost > Decimal(str(limits['max_cost']))):
                raise ModelError('budget', '模型调用次数、token预留或估算费用预算不足，已在请求前停止')
            same_round = [row for row in rows if row['round'] == round_number]
            if same_round and same_round[-1]['status'] in ('reserved', 'unknown', 'succeeded'):
                raise ModelError('uncertain', '该轮已有调用或结果，禁止重复发起')
            attempt = len(same_round) + 1
            if attempt > limits['max_retries'] + 1:
                raise ModelError('retry_budget', '本轮模型重试预算已耗尽')
            call_id = uuid.uuid4().hex
            body = dict(provider=config['provider'], config=config, request=request, request_sha256=digest(request),
                        reserved_input_tokens=input_bound, reserved_output_tokens=output_bound,
                        charged_tokens=tokens, charged_cost=str(cost), usage_unknown=True)
            db.execute('INSERT INTO model_calls VALUES (?,?,?,?,?,?,?)',
                       (call_id, job_id, round_number, attempt, 'reserved', datetime.now(timezone.utc).isoformat(), canonical(body)))
            self.store._event(db, job_id, 'model_reserved', {'call_id': call_id, 'round': round_number,
                                    'provider': config['provider'], 'model': config['model'], 'reserved_tokens': tokens,
                                    'estimated_cost': float(cost), 'currency': config['currency']})
        return self.records(job_id)[-1]

    def finish(self, job_id, owner, call_id, *, response=None, error=None):
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.store._lease(db, job_id, owner)
            row = db.execute('SELECT status,body FROM model_calls WHERE id=? AND job_id=?', (call_id, job_id)).fetchone()
            if not row or row[0] != 'reserved':
                raise ModelError('uncertain', '调用所有权或状态改变，未覆盖调用记录')
            body = json.loads(row[1])
            if response:
                body.update(response)
                body['charged_tokens'] = response['input_tokens'] + response['output_tokens']
                body['charged_cost'] = str(estimated_cost(body['config'], response['input_tokens'], response['output_tokens']))
                body['usage_unknown'] = False
                if (response['input_tokens'] > body['reserved_input_tokens'] or
                        response['output_tokens'] > body['reserved_output_tokens']):
                    error = ModelError('usage_limit', '供应商报告用量超过本地预留，已按报告记账并停止后续调用')
                elif response['error']:
                    error = ModelError('proposal_format', response['error'])
            status = 'failed' if error else 'succeeded'
            if error:
                body.update(category=error.category, error=str(error), retryable=error.retryable, http_status=error.http_status)
                body.pop('proposal', None)
            db.execute('UPDATE model_calls SET status=?,body=? WHERE id=?', (status, canonical(body), call_id))
            self.store._event(db, job_id, 'model_' + status, {'call_id': call_id, 'category': body.get('category'),
                                       'charged_tokens': body['charged_tokens'], 'estimated_cost': float(body['charged_cost'])})
        return next(row for row in self.records(job_id) if row['id'] == call_id)

    @staticmethod
    def interrupt(db, job_id):
        # Preserve the full reservation, not a fabricated zero bill.
        rows = db.execute("SELECT id,body FROM model_calls WHERE job_id=? AND status='reserved'", (job_id,)).fetchall()
        for call_id, encoded in rows:
            body = json.loads(encoded)
            body.update(category='interrupted', error='本地执行停止，远端结果/用量未知；保留预留，不自动重发', retryable=False)
            db.execute("UPDATE model_calls SET status='unknown',body=? WHERE id=?", (canonical(body), call_id))


class APIProposalProvider:
    def __init__(self, store, job_id, owner, config, goal, transport=None):
        self.store, self.job_id, self.owner, self.config, self.goal = store, job_id, owner, config, goal
        self.calls, self.transport = store.model_calls, transport or ModelHTTP()

    def propose(self, context, feedback):
        round_number = self.store.job(self.job_id)['progress']['rounds'] + 1
        package = compact_context(context, feedback, self.goal)
        request = request_body(self.config, package, self.config['limits'])
        records = [row for row in self.calls.records(self.job_id) if row['round'] == round_number]
        if records:
            latest = records[-1]
            same_request = (latest['request_sha256'] == digest(request) or
                            request_without_live_counts(self.config['provider'], latest['request']) ==
                            request_without_live_counts(self.config['provider'], request))
            if not same_request or latest['config'] != self.config:
                raise ModelError('context_changed', '已记录调用的上下文或配置改变，拒绝静默重发')
            # A resumed retry uses the exact original request, including its census snapshot.
            request = latest['request']
            if latest['status'] == 'succeeded':
                return latest['proposal']
            if latest['status'] in ('reserved', 'unknown'):
                raise ModelError('uncertain', '本轮远端结果未知；不自动重发，可核对后显式新建重试')
            if not latest.get('retryable') or latest['attempt'] > self.config['limits']['max_retries']:
                raise ModelError(latest.get('category', 'model'), latest.get('error', '模型调用失败'))
        credential(self.config['provider'])  # Missing keys must not consume a paid-call reservation.
        while True:
            call = self.calls.reserve(self.job_id, self.owner, round_number, self.config, request)
            try:
                raw = self.transport.post(self.config['provider'], request, self.config['limits']['request_timeout_seconds'])
                response = decode_response(self.config['provider'], raw)
            except ModelError as error:
                self.calls.finish(self.job_id, self.owner, call['id'], error=error)
                if error.retryable and call['attempt'] <= self.config['limits']['max_retries']:
                    continue  # Every retry obtains a separate reservation; no ambiguous timeout retry.
                raise
            result = self.calls.finish(self.job_id, self.owner, call['id'], response=response)
            if result['status'] != 'succeeded':
                raise ModelError(result['category'], result['error'])
            return result['proposal']
