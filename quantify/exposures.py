"""Shared atomic holdout exposure reservations for single-trade and portfolio validation."""
import json
from contextlib import closing
from datetime import datetime, timezone

from .storage import canonical


def initialize(store):
    with closing(store.connect()) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS final_exposures (
            freeze_id TEXT PRIMARY KEY, start TEXT, end TEXT, symbols TEXT, created_at TEXT)''')


def reserve(store, key, start, end, symbols):
    """Reserve before computing labels. Keep abandoned reservations across retries/restarts."""
    initialize(store)
    universe = set(symbols)
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT freeze_id,start,end,symbols FROM final_exposures').fetchall()
        if any(r[0] == key for r in previous):
            raise ValueError('此冻结版本已开始验收；结果未提交时禁止重算或重新获得独立验收资格')
        overlaps = [r[0] for r in previous if r[1] <= end and r[2] >= start and universe & set(json.loads(r[3]))]
        # Also account for known completed development studies, across snapshots and engines.
        # Read in the same transaction; only dates/symbols affect the independence decision.
        snapshots = {}
        for experiment_id, encoded in db.execute("SELECT id,body FROM artifacts WHERE kind='experiment'"):
            experiment = json.loads(encoded)
            protocol = experiment.get('protocol', {})
            if experiment.get('status') != 'completed' or protocol.get('development_end', '') < start:
                continue
            first = experiment['report']['data_window'][0]
            if first > end:
                continue
            snapshot_id = experiment['request']['snapshot_id']
            if snapshot_id not in snapshots:
                row = db.execute("SELECT body FROM artifacts WHERE kind='snapshot' AND id=?", (snapshot_id,)).fetchone()
                snapshots[snapshot_id] = set(json.loads(row[0])['symbols']) if row else set()
            if universe & snapshots[snapshot_id]:
                overlaps.append('development:' + experiment_id)
        db.execute('INSERT INTO final_exposures VALUES (?,?,?,?,?)',
                   (key, start, end, canonical(sorted(universe)), datetime.now(timezone.utc).isoformat()))
    return sorted(overlaps)
