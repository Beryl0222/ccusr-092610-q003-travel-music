"""后台任务处理：下架期限执行与更正传播。

任务持久化在 jobs 表中，认领与业务效果在同一事务提交；
进程中断后，新实例通过 recover_stale 回收超时未完成的任务继续处理，
已提交的效果不会重复执行（处理器按当前状态幂等判断）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from .models import (
    ASSET_STATE_REGISTERED,
    ASSET_STATE_WITHDRAWN,
    JOB_CORRECTION_PROPAGATION,
    JOB_ENFORCE_REPLACEMENT,
    JOB_STATUS_CLAIMED,
    JOB_STATUS_DONE,
    JOB_STATUS_PENDING,
    JOB_TAKEDOWN_ENFORCE,
    RELEASE_STATE_LIVE,
    RELEASE_STATE_SUSPENDED,
    REVISION_STATE_BLOCKED,
    REVISION_STATE_DRAFT,
    REVISION_STATE_READY,
    TASK_STATE_OPEN,
    StateError,
)
from .service import ProvenanceService, _iso, _parse


class JobWorker:
    """拉取到期任务并执行；可安全地在任意时刻中断与重启。"""

    def __init__(self, service: ProvenanceService, worker_id: str | None = None) -> None:
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"

    def recover_stale(self, now: datetime, claim_timeout_seconds: int = 300) -> int:
        """回收认领后超时未完成的任务（例如进程中断遗留的）。"""
        threshold = now - timedelta(seconds=claim_timeout_seconds)
        recovered = 0
        with self.service.store.transaction() as conn:
            claimed = conn.execute(
                "SELECT job_id, claimed_at FROM jobs WHERE status = ?", (JOB_STATUS_CLAIMED,)
            ).fetchall()
            for row in claimed:
                if _parse(row["claimed_at"]) < threshold:
                    conn.execute(
                        "UPDATE jobs SET status = ?, claimed_by = NULL, claimed_at = NULL"
                        " WHERE job_id = ?",
                        (JOB_STATUS_PENDING, row["job_id"]),
                    )
                    recovered += 1
        return recovered

    def run_pending(self, now: datetime, limit: int = 100) -> list[str]:
        """执行所有到期任务，返回处理过的任务标识。"""
        processed = []
        for _ in range(limit):
            job_id = self._process_one(now)
            if job_id is None:
                break
            processed.append(job_id)
        return processed

    def _process_one(self, now: datetime) -> str | None:
        with self.service.store.transaction() as conn:
            pending = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY due_at, job_id",
                (JOB_STATUS_PENDING,),
            ).fetchall()
            row = next((job for job in pending if _parse(job["due_at"]) <= now), None)
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, claimed_by = ?, claimed_at = ?,"
                " attempts = attempts + 1 WHERE job_id = ?",
                (JOB_STATUS_CLAIMED, self.worker_id, _iso(now), row["job_id"]),
            )
            handler = self._handlers().get(row["job_type"])
            if handler is None:
                raise StateError(f"未知任务类型: {row['job_type']}")
            handler(conn, json.loads(row["payload"]), now)
            conn.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?", (JOB_STATUS_DONE, row["job_id"])
            )
            return row["job_id"]

    def _handlers(self):
        return {
            JOB_ENFORCE_REPLACEMENT: self._enforce_replacement,
            JOB_TAKEDOWN_ENFORCE: self._enforce_takedown,
            JOB_CORRECTION_PROPAGATION: self._propagate_correction,
        }

    # ------------------------------------------------------------------
    # 替换任务逾期：挂起仍在在线的发布
    # ------------------------------------------------------------------
    def _enforce_replacement(self, conn, payload: dict, now: datetime) -> None:
        task = conn.execute(
            "SELECT * FROM replacement_tasks WHERE task_id = ?", (payload["task_ref"],)
        ).fetchone()
        if task is None or task["state"] != TASK_STATE_OPEN:
            return
        release = conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (task["release_ref"],)
        ).fetchone()
        if release is None or release["state"] != RELEASE_STATE_LIVE:
            return
        conn.execute(
            "UPDATE releases SET state = ? WHERE release_id = ?",
            (RELEASE_STATE_SUSPENDED, release["release_id"]),
        )
        self.service._emit(
            conn, "RELEASE_SUSPENDED", "channel_release", release["release_id"], now,
            {
                "reason": "来源撤回后替换任务逾期未完成",
                "task_ref": task["task_id"],
            },
        )

    # ------------------------------------------------------------------
    # 下架期限到达：执行下架；已结束活动保留证据不动
    # ------------------------------------------------------------------
    def _enforce_takedown(self, conn, payload: dict, now: datetime) -> None:
        takedown = conn.execute(
            "SELECT * FROM takedowns WHERE takedown_id = ?", (payload["takedown_ref"],)
        ).fetchone()
        if takedown is None or takedown["state"] != "open":
            return
        affected_releases: list[str] = []
        affected_revisions: list[str] = []
        if takedown["target_kind"] == "release":
            release = conn.execute(
                "SELECT * FROM releases WHERE release_id = ?", (takedown["target_ref"],)
            ).fetchone()
            if release is not None and release["state"] == RELEASE_STATE_LIVE:
                self._suspend_release(conn, release["release_id"], takedown["reason"], now)
                affected_releases.append(release["release_id"])
        else:
            asset_ref = takedown["target_ref"]
            asset = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_ref,)
            ).fetchone()
            if asset is not None and asset["state"] == ASSET_STATE_REGISTERED:
                conn.execute(
                    "UPDATE assets SET state = ? WHERE asset_id = ?",
                    (ASSET_STATE_WITHDRAWN, asset_ref),
                )
            affected_revisions.extend(
                self.service._block_unpublished_revisions(conn, asset_ref, takedown["reason"], now)
            )
            live_releases = conn.execute(
                "SELECT * FROM releases WHERE state = ?", (RELEASE_STATE_LIVE,)
            ).fetchall()
            for release in live_releases:
                snapshot = json.loads(release["frozen_snapshot"])
                if any(item.get("asset_ref") == asset_ref for item in snapshot["items"]):
                    self._suspend_release(conn, release["release_id"], takedown["reason"], now)
                    affected_releases.append(release["release_id"])
        conn.execute(
            "UPDATE takedowns SET state = 'enforced' WHERE takedown_id = ?",
            (takedown["takedown_id"],),
        )
        self.service._emit(
            conn, "TAKEDOWN_ENFORCED", "takedown_notice", takedown["takedown_id"], now,
            {
                "action": "enforced",
                "affected_releases": sorted(affected_releases),
                "affected_revisions": sorted(affected_revisions),
            },
        )

    def _suspend_release(self, conn, release_id: str, reason: str, now: datetime) -> None:
        conn.execute(
            "UPDATE releases SET state = ? WHERE release_id = ?",
            (RELEASE_STATE_SUSPENDED, release_id),
        )
        self.service._emit(
            conn, "RELEASE_SUSPENDED", "channel_release", release_id, now,
            {"reason": reason},
        )

    # ------------------------------------------------------------------
    # 更正传播到渠道
    # ------------------------------------------------------------------
    def _propagate_correction(self, conn, payload: dict, now: datetime) -> None:
        correction = conn.execute(
            "SELECT * FROM corrections WHERE correction_id = ?", (payload["correction_ref"],)
        ).fetchone()
        if correction is None:
            return
        release = conn.execute(
            "SELECT * FROM releases WHERE release_id = ?", (correction["release_ref"],)
        ).fetchone()
        if release is None:
            return
        existing = conn.execute(
            "SELECT 1 FROM correction_propagations WHERE correction_ref = ? AND channel = ?",
            (correction["correction_id"], release["channel"]),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO correction_propagations(correction_ref, channel, propagated_at)"
            " VALUES (?, ?, ?)",
            (correction["correction_id"], release["channel"], _iso(now)),
        )
        self.service._emit(
            conn, "CORRECTION_PROPAGATED", "channel_release", release["release_id"], now,
            {
                "release_ref": release["release_id"],
                "channel": release["channel"],
                "correction_ref": correction["correction_id"],
            },
        )
