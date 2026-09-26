"""SQLite 持久化：事件流与投影表在同一事务写入，支撑中断后恢复。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_aggregate
  ON events(aggregate_type, aggregate_id, version);

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  asset_kind TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  generation_params TEXT,
  declared_scope TEXT,
  portrait_subjects TEXT,
  declared_by TEXT NOT NULL,
  state TEXT NOT NULL,
  prior_asset_ref TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprint_registry (
  content_hash TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  scope_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
  grant_id TEXT PRIMARY KEY,
  asset_ref TEXT NOT NULL,
  grant_kind TEXT NOT NULL,
  grantor_ref TEXT NOT NULL,
  declared_by TEXT NOT NULL,
  scope TEXT NOT NULL,
  state TEXT NOT NULL,
  verified_by TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contributions (
  contribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_ref TEXT NOT NULL,
  contributor_ref TEXT NOT NULL,
  contribution_role TEXT NOT NULL,
  declared_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
  revision_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  items TEXT NOT NULL,
  created_by TEXT NOT NULL,
  fact_signed_by TEXT,
  commercial_signed_by TEXT,
  blocked_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS track_locks (
  track_ref TEXT PRIMARY KEY,
  revision_ref TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
  release_id TEXT PRIMARY KEY,
  revision_ref TEXT NOT NULL,
  channel TEXT NOT NULL,
  state TEXT NOT NULL,
  frozen_snapshot TEXT NOT NULL,
  notices TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  ended_at TEXT
);

CREATE TABLE IF NOT EXISTS replacement_tasks (
  task_id TEXT PRIMARY KEY,
  release_ref TEXT NOT NULL,
  asset_ref TEXT NOT NULL,
  channel TEXT NOT NULL,
  scope TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  due_at TEXT,
  resolution TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS takedowns (
  takedown_id TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  reason TEXT NOT NULL,
  state TEXT NOT NULL,
  due_at TEXT NOT NULL,
  opened_by TEXT NOT NULL,
  opened_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
  correction_id TEXT PRIMARY KEY,
  release_ref TEXT NOT NULL,
  correction TEXT NOT NULL,
  issued_by TEXT NOT NULL,
  issued_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS correction_propagations (
  correction_ref TEXT NOT NULL,
  channel TEXT NOT NULL,
  propagated_at TEXT NOT NULL,
  PRIMARY KEY (correction_ref, channel)
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL,
  claimed_by TEXT,
  claimed_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, due_at);
"""


class Store:
    """单连接存储；写事务以 BEGIN IMMEDIATE 开始，配合 busy_timeout 串行化写者。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._write_lock = threading.Lock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文；任何异常都会整体回滚，保证原子性。"""
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def read_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._conn.close()
