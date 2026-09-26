"""SQLite 持久化：事件日志、业务状态与后台任务。

所有多步写入都包在 ``atomic()`` 的 ``BEGIN IMMEDIATE`` 事务里，
曲目锁靠主键冲突实现跨连接原子占用；后台任务持久化在 ``jobs``
表中，进程中断后重开库即可继续处理。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  occurred_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  generation_params TEXT,
  license_scope TEXT,
  required_grants TEXT NOT NULL,
  status TEXT NOT NULL,
  declared_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fingerprint_index (
  content_hash TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  scope_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contributions (
  contribution_id TEXT PRIMARY KEY,
  creator_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  role TEXT NOT NULL,
  note TEXT,
  declared_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
  grant_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  grant_kind TEXT NOT NULL,
  grantor_ref TEXT NOT NULL,
  scope TEXT NOT NULL,
  status TEXT NOT NULL,
  declared_by TEXT NOT NULL,
  confirmed_by TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
  revision_id TEXT PRIMARY KEY,
  items TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by TEXT NOT NULL,
  fact_signed_by TEXT,
  commercial_signed_by TEXT
);
CREATE TABLE IF NOT EXISTS track_locks (
  track_asset_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
  release_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  status TEXT NOT NULL,
  posted_by TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  campaign_id TEXT
);
CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  closed_at TEXT,
  evidence TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  scope TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS takedowns (
  takedown_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS propagations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  release_id TEXT NOT NULL,
  notice TEXT NOT NULL,
  propagated_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    @contextmanager
    def atomic(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()
