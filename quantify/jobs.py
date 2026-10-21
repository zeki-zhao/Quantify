"""Single-owner, durable local queue with one bounded worker process at a time."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .storage import ResearchStore, canonical
from .model_calls import ModelCalls

ACTIVE = ('queued', 'running')
TERMINAL = ('succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted')


def now():
    return datetime.now(timezone.utc).isoformat()


class LostLease(RuntimeError):
    pass


class JobStore(ResearchStore):
    def __init__(self, directory):
        super().__init__(directory)
        with closing(self.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL, progress TEXT NOT NULL, result TEXT,
                error TEXT, timeout_seconds REAL NOT NULL, elapsed_seconds REAL NOT NULL DEFAULT 0,
                stop_requested TEXT, owner TEXT, retry_of TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS job_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                created_at TEXT NOT NULL, event TEXT NOT NULL, body TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS job_event_index ON job_events(job_id, sequence)')
        self.model_calls = ModelCalls(self)

    def job(self, job_id):
        if not isinstance(job_id, str):
            raise ValueError('任务编号必须是文本')
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        value = dict(row)
        for key in ('payload', 'progress', 'result'):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        return value

    def jobs(self):
        with closing(self.connect()) as db:
            ids = db.execute('SELECT id FROM jobs ORDER BY created_at DESC, id').fetchall()
        return [self.job(row[0]) for row in ids]

    def events(self, job_id):
        self.job(job_id)
        with closing(self.connect()) as db:
            rows = db.execute('SELECT sequence,created_at,event,body FROM job_events WHERE job_id=? ORDER BY sequence', (job_id,)).fetchall()
        return [dict(sequence=row[0], created_at=row[1], event=row[2], body=json.loads(row[3])) for row in rows]

    def _event(self, db, job_id, event, body):
        db.execute('INSERT INTO job_events(job_id,created_at,event,body) VALUES (?,?,?,?)',
                   (job_id, now(), event, canonical(body)))

    def event(self, job_id, event, body, owner=None):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if owner:
                self._lease(db, job_id, owner)
            self._event(db, job_id, event, body)

    def _lease(self, db, job_id, owner):
        row = db.execute('SELECT status,owner FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or row != ('running', owner):
            raise LostLease('任务已经停止或执行所有者已改变')

    def create(self, kind, payload, timeout_seconds, progress=None, status='queued', retry_of=None):
        if kind not in ('experiment', 'research', 'history', 'portfolio', 'diagnostic', 'increment', 'portfolio_validation', 'pipeline') or status not in ('queued', 'waiting_input'):
            raise ValueError('任务类型或初始状态无效')
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600:
            raise ValueError('任务运行时限需大于零且最多 3600 秒')
        job_id = uuid.uuid4().hex
        stamp = now()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if kind == 'pipeline':
                active=db.execute("SELECT payload FROM jobs WHERE kind='pipeline' AND status IN ('queued','running')").fetchall()
                if any(json.loads(r[0]).get('pipeline_id')==payload.get('pipeline_id') for r in active):
                    raise ValueError('该流水线已有待执行任务')
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','waiting_input','paused')").fetchone()[0]
            if count >= 50:
                raise ValueError('最多保留 50 个未结束任务，请取消不用的任务')
            db.execute('''INSERT INTO jobs(id,kind,status,payload,progress,timeout_seconds,retry_of,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?)''',
                       (job_id, kind, status, canonical(payload), canonical(progress or {}), timeout_seconds, retry_of, stamp, stamp))
            self._event(db, job_id, 'created', {'kind': kind, 'status': status, 'retry_of': retry_of})
        return self.job(job_id)

    def claim(self, owner):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                return None
            job_id = row[0]
            db.execute("UPDATE jobs SET status='running',owner=?,stop_requested=NULL,updated_at=? WHERE id=?", (owner, now(), job_id))
            self._event(db, job_id, 'running', {})
        return self.job(job_id)

    def checkpoint(self, job_id, owner, progress):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._lease(db, job_id, owner)
            db.execute('UPDATE jobs SET progress=?,updated_at=? WHERE id=?', (canonical(progress), now(), job_id))

    def finish(self, job_id, owner, status, result=None, error=None):
        if status not in (*TERMINAL, 'waiting_input', 'paused'):
            raise ValueError('结束状态无效')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._lease(db, job_id, owner)
            stop = db.execute('SELECT stop_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            status = stop or status
            self.model_calls.interrupt(db, job_id)
            db.execute('UPDATE jobs SET status=?,result=?,error=?,stop_requested=NULL,updated_at=? WHERE id=?',
                       (status, canonical(result) if result is not None else None, error, now(), job_id))
            self._event(db, job_id, status, {'error': error})

    def add_elapsed(self, job_id, elapsed):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE jobs SET elapsed_seconds=elapsed_seconds+? WHERE id=?', (elapsed, job_id))

    def control(self, job_id, action):
        if not isinstance(job_id, str):
            raise ValueError('任务编号必须是文本')
        if action not in ('cancel', 'pause', 'resume'):
            raise ValueError('仅支持取消、暂停或继续')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT kind,status,payload,progress,elapsed_seconds,timeout_seconds FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            kind, state, payload, progress, elapsed, limit = row
            if state in TERMINAL:
                raise ValueError('已结束任务不能修改；重试会创建新任务')
            if action == 'resume':
                if state != 'paused' or elapsed >= limit:
                    raise ValueError('只能继续尚有运行时间预算的暂停任务')
                payload, progress = json.loads(payload), json.loads(progress)
                state = ('queued' if payload.get('provider', 'manual') != 'manual' or progress.get('pending') or progress.get('cursor', 0) < len(payload.get('proposals', [])) else 'waiting_input')
                db.execute('UPDATE jobs SET status=?,stop_requested=NULL,updated_at=? WHERE id=?', (state, now(), job_id))
            else:
                if action == 'pause' and kind != 'research':
                    raise ValueError('单次实验只能取消；研究任务支持暂停')
                target = 'cancelled' if action == 'cancel' else 'paused'
                if state == 'running':
                    db.execute('UPDATE jobs SET stop_requested=?,updated_at=? WHERE id=?', (target, now(), job_id))
                else:
                    db.execute('UPDATE jobs SET status=?,updated_at=? WHERE id=?', (target, now(), job_id))
            self._event(db, job_id, 'control', {'action': action})
        return self.job(job_id)

    def append_proposals(self, job_id, proposals):
        if not isinstance(job_id, str):
            raise ValueError('任务编号必须是文本')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT kind,status,payload,progress FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row[0] != 'research' or row[1] not in ('waiting_input', 'paused'):
                raise ValueError('仅等待提案或暂停的研究任务可以追加提案')
            payload, progress = json.loads(row[2]), json.loads(row[3])
            if payload.get('provider', 'manual') != 'manual':
                raise ValueError('API会话由固定提供方生成提案，不能导入手动提案')
            if len(payload['proposals']) + len(proposals) > payload['max_rounds']:
                raise ValueError('提案总数超过本任务轮次预算')
            payload['proposals'].extend(proposals)
            state = 'paused' if row[1] == 'paused' else 'queued'
            db.execute('UPDATE jobs SET payload=?,status=?,updated_at=? WHERE id=?', (canonical(payload), state, now(), job_id))
            self._event(db, job_id, 'proposals_imported', {'count': len(proposals), 'provider': 'manual'})
        return self.job(job_id)

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            ids = db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchall()
            for (job_id,) in ids:
                self.model_calls.interrupt(db, job_id)
                db.execute("UPDATE jobs SET status='interrupted',stop_requested=NULL,error=?,updated_at=? WHERE id=?",
                           ('服务重启；未自动重跑，可显式创建重试任务', now(), job_id))
                self._event(db, job_id, 'interrupted', {'reason': 'restart'})


class QueueManager:
    def __init__(self, directory):
        self.store = JobStore(directory)
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.lock = open(self.store.directory / 'queue.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lock.seek(0)
                if not self.lock.read(1):
                    self.lock.write(b'0'); self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError('此数据目录已有队列服务，请复用该服务或更换数据目录') from None
        self.store.recover()
        self.thread = threading.Thread(target=self._loop, name='quantify-queue', daemon=True)
        self.thread.start()

    def _spawn(self, job):
        return subprocess.Popen([sys.executable, '-m', 'quantify.worker', str(self.store.directory.resolve()), job['id'], self.owner],
                                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)

    def _run(self, job):
        start = time.monotonic()
        process = None
        try:
            if job['elapsed_seconds'] >= job['timeout_seconds']:
                self.store.finish(job['id'], self.owner, 'timed_out', error='累计运行时间预算已耗尽')
                return
            process = self._spawn(job)
            while process.poll() is None:
                current = self.store.job(job['id'])
                elapsed = time.monotonic() - start + job['elapsed_seconds']
                target = ('interrupted' if self.stop.is_set() else current['stop_requested'] or
                          ('timed_out' if elapsed >= job['timeout_seconds'] else None))
                if target:
                    self._terminate(process)
                    latest = self.store.job(job['id'])
                    if latest['status'] == 'running':
                        self.store.finish(job['id'], self.owner, target, error='运行时限到达' if target == 'timed_out' else None)
                    break
                self.stop.wait(.05)
            current = self.store.job(job['id'])
            if current['status'] == 'running':
                self.store.finish(job['id'], self.owner, 'failed', error=f'计算进程异常退出（状态 {process.returncode}）')
        except (OSError, ValueError, sqlite3.Error) as exc:
            if process:
                self._terminate(process)
            try:
                self.store.finish(job['id'], self.owner, 'failed', error=f'队列执行失败：{type(exc).__name__}')
            except LostLease:
                pass
        finally:
            self.store.add_elapsed(job['id'], time.monotonic() - start)

    def _loop(self):
        while not self.stop.is_set():
            job = self.store.claim(self.owner)
            if job:
                self._run(job)
            else:
                self.stop.wait(.1)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('队列尚未停止')
        if not self.lock.closed:
            if os.name == 'nt':
                import msvcrt
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock.close()
