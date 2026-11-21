"""Durable, bounded idempotency cache for paid assistant requests.

The local service can restart while an assistant response is in flight. It then
cannot know whether the provider was already charged. Keeping the final response
(keyed by kind, epoch and job id) lets the local service re-fetch it after restart
without calling the provider again. Entries are short-lived and local-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

TTL_MS = 60 * 60 * 1000
MAX_ENTRIES = 4096


class AiOutcomeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS outcomes("
                "kind TEXT NOT NULL, epoch TEXT NOT NULL, job_id TEXT NOT NULL,"
                "expires_at INTEGER NOT NULL, http_status INTEGER NOT NULL, body TEXT NOT NULL,"
                "PRIMARY KEY(kind, epoch, job_id))"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def get(self, kind: str, epoch: str, job_id: str, now: int):
        with self._connect() as db:
            row = db.execute(
                "SELECT http_status, body FROM outcomes WHERE kind=?1 AND epoch=?2 AND job_id=?3"
                " AND expires_at>=?4",
                (kind, epoch, job_id, now),
            ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1])

    def put(self, kind: str, epoch: str, job_id: str, now: int, status: int, body: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO outcomes(kind,epoch,job_id,expires_at,http_status,body)"
                " VALUES(?1,?2,?3,?4,?5,?6)"
                " ON CONFLICT(kind,epoch,job_id) DO UPDATE SET expires_at=excluded.expires_at,"
                " http_status=excluded.http_status, body=excluded.body",
                (kind, epoch, job_id, now + TTL_MS, status, body),
            )
            db.execute("DELETE FROM outcomes WHERE expires_at<?1", (now,))
            db.execute(
                "DELETE FROM outcomes WHERE rowid IN ("
                "SELECT rowid FROM outcomes ORDER BY rowid DESC LIMIT -1 OFFSET ?1)",
                (MAX_ENTRIES,),
            )
