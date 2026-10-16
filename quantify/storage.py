"""Local immutable research artifacts; SQLite is part of Python's standard library."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class ResearchStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'research.sqlite3'
        with closing(self.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS artifacts (
                kind TEXT NOT NULL, id TEXT NOT NULL, created_at TEXT NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY (kind, id))''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def put(self, kind, body, artifact_id=None):
        artifact_id = artifact_id or digest(body)
        encoded = canonical(body)
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?)',
                       (kind, artifact_id, datetime.now(timezone.utc).isoformat(), encoded))
            saved = db.execute('SELECT body FROM artifacts WHERE kind=? AND id=?',
                               (kind, artifact_id)).fetchone()[0]
            if saved != encoded:
                raise ValueError('不可覆盖已有版本')
        return self.get(kind, artifact_id)

    def get(self, kind, artifact_id):
        if not isinstance(artifact_id, str):
            raise ValueError('记录编号无效')
        with closing(self.connect()) as db:
            row = db.execute('SELECT id, created_at, body FROM artifacts WHERE kind=? AND id=?',
                             (kind, artifact_id)).fetchone()
        if row is None:
            raise ValueError('记录不存在')
        body = json.loads(row[2])
        if kind in ('snapshot', 'factor', 'freeze', 'portfolio_spec') and digest(body) != row[0]:
            raise ValueError('记录摘要校验失败')
        return {'id': row[0], 'created_at': row[1], **body}

    def list(self, kind):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id FROM artifacts WHERE kind=? ORDER BY created_at DESC, id',
                              (kind,)).fetchall()
        return [self.get(kind, row[0]) for row in rows]
