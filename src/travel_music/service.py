"""素材谱系服务。

在基础事件契约之上实现发行团队要求的业务规则：

- 作者只能申报自己的创作与权利，不能替他人确认权利；
- 事实核验与商业发布由不同角色、不同人员签署；
- 渠道发布时冻结实际采用的画面、声音、说明与限定（notices 以结构化数据随行，
  平台二次剪辑截掉片尾字幕也不会丢失）；
- 来源撤回只阻止未发布版本，对在线版本生成范围明确的替换任务，已结束活动保留当时证据；
- 相同素材指纹的重复提交沿用原决定；指纹相同但来源或授权范围漂移时隔离审查；
- 多个剪辑方案同时占用同一首曲目时以事务原子锁定，失败整体回滚；
- 生成参数仅作谱系记录，不作为版权证明；
- 下架期限与更正传播由持久化后台任务处理，中断后可恢复（见 worker.py）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import validate_event
from .models import (
    ASSET_KIND_AUDIO,
    ASSET_KIND_VIDEO,
    ASSET_KINDS,
    ASSET_STATE_QUARANTINED,
    ASSET_STATE_REGISTERED,
    ASSET_STATE_REJECTED,
    ASSET_STATE_WITHDRAWN,
    CONTRIBUTION_ROLES,
    GRANT_KIND_COMPOSITION,
    GRANT_KIND_PORTRAIT,
    GRANT_KINDS,
    GRANT_STATE_DECLARED,
    GRANT_STATE_VERIFIED,
    ITEM_KIND_AUDIO,
    ITEM_KIND_DESCRIPTION,
    ITEM_KIND_VIDEO,
    ITEM_KINDS,
    JOB_CORRECTION_PROPAGATION,
    JOB_ENFORCE_REPLACEMENT,
    JOB_STATUS_PENDING,
    JOB_TAKEDOWN_ENFORCE,
    RELEASE_STATE_ENDED,
    RELEASE_STATE_LIVE,
    RELEASE_STATE_SUSPENDED,
    REVISION_ACTIVE_STATES,
    REVISION_STATE_ABANDONED,
    REVISION_STATE_BLOCKED,
    REVISION_STATE_DRAFT,
    REVISION_STATE_READY,
    REVISION_STATE_RELEASED,
    ROLE_FACT_CHECKER,
    ROLE_RELEASE_MANAGER,
    ROLE_RIGHTS_OFFICER,
    SIGNOFF_COMMERCIAL,
    SIGNOFF_FACT,
    SOURCE_AI_GENERATED,
    SOURCE_KINDS,
    TASK_STATE_DONE,
    TASK_STATE_OPEN,
    Actor,
    ChannelPolicy,
    ConflictError,
    ContractViolation,
    NotFoundError,
    PermissionDenied,
    RegistrationResult,
    ServiceError,
    StateError,
)
from .store import Store


def load_schema(path: str | Path | None = None) -> dict:
    schema_path = Path(path) if path else Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须携带时区")
    return value.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ProvenanceService:
    """素材谱系服务入口；所有写操作在单事务内完成事件追加与投影更新。"""

    def __init__(
        self,
        store: Store,
        channel_policies: Sequence[ChannelPolicy] = (),
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.schema = schema if schema is not None else load_schema()
        self.policies = {policy.channel: policy for policy in channel_policies}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _emit(self, conn, event_type: str, aggregate_type: str, aggregate_id: str,
              occurred_at: datetime, payload: Mapping[str, Any]) -> dict:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM events "
            "WHERE aggregate_type = ? AND aggregate_id = ?",
            (aggregate_type, aggregate_id),
        ).fetchone()
        event = {
            "event_id": _new_id("evt"),
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _iso(occurred_at),
            "version": row["v"],
            "payload": dict(payload),
        }
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolation(issues)
        conn.execute(
            "INSERT INTO events(event_id, event_type, aggregate_type, aggregate_id,"
            " occurred_at, version, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event["event_id"],
                event_type,
                aggregate_type,
                aggregate_id,
                event["occurred_at"],
                event["version"],
                _canon(event["payload"]),
            ),
        )
        return event

    def _enqueue(self, conn, job_type: str, payload: Mapping[str, Any], due_at: datetime) -> str:
        job_id = _new_id("job")
        conn.execute(
            "INSERT INTO jobs(job_id, job_type, payload, due_at, status, attempts)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            (job_id, job_type, _canon(payload), _iso(due_at), JOB_STATUS_PENDING),
        )
        return job_id

    @staticmethod
    def _require_role(actor: Actor, role: str) -> None:
        if not actor.has(role):
            raise PermissionDenied(f"操作需要角色 {role}")

    def _get_asset(self, conn, asset_id: str):
        row = conn.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"素材不存在: {asset_id}")
        return row

    def _get_revision(self, conn, revision_id: str):
        row = conn.execute("SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"剪辑版本不存在: {revision_id}")
        return row

    def _get_release(self, conn, release_id: str):
        row = conn.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"渠道发布不存在: {release_id}")
        return row

    def _policy_for(self, channel: str) -> ChannelPolicy:
        return self.policies.get(channel, ChannelPolicy(channel=channel))

    # ------------------------------------------------------------------
    # 素材注册：指纹幂等、漂移隔离
    # ------------------------------------------------------------------
    def register_asset(
        self,
        actor: Actor,
        asset_kind: str,
        content_hash: str,
        source_kind: str,
        *,
        generation_params: Mapping[str, Any] | None = None,
        declared_scope: Mapping[str, Any] | None = None,
        portrait_subjects: Sequence[str] | None = None,
        now: datetime,
    ) -> RegistrationResult:
        """登记拍摄片段或音乐素材。

        相同指纹且来源与授权范围一致时沿用原决定；指纹相同但来源或授权
        范围漂移时登记为隔离状态，等待人工审查。
        """
        _iso(now)
        if asset_kind not in ASSET_KINDS:
            raise ServiceError(f"未知素材类别: {asset_kind}")
        if source_kind not in SOURCE_KINDS:
            raise ServiceError(f"未知素材来源: {source_kind}")
        if not content_hash or not content_hash.strip():
            raise ServiceError("素材指纹不能为空")
        if generation_params is not None and source_kind != SOURCE_AI_GENERATED:
            raise ServiceError("只有 AI 生成素材可携带生成参数摘要")
        if portrait_subjects and asset_kind != ASSET_KIND_VIDEO:
            raise ServiceError("只有画面素材可登记肖像主体")
        scope_key = _canon(declared_scope) if declared_scope is not None else ""
        with self.store.transaction() as conn:
            registry = conn.execute(
                "SELECT * FROM fingerprint_registry WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if registry is not None:
                original = self._get_asset(conn, registry["asset_id"])
                if registry["source_kind"] == source_kind and registry["scope_key"] == scope_key:
                    self._emit(
                        conn, "ASSET_REGISTRATION_DEDUPED", "media_asset", original["asset_id"], now,
                        {
                            "content_hash": content_hash,
                            "reused_asset_ref": original["asset_id"],
                            "decision": original["state"],
                            "attempted_by": actor.actor_id,
                        },
                    )
                    return RegistrationResult(original["asset_id"], original["state"], reused=True)
                drift = []
                if registry["source_kind"] != source_kind:
                    drift.append("source_kind")
                if registry["scope_key"] != scope_key:
                    drift.append("declared_scope")
                asset_id = _new_id("asset")
                self._insert_asset(
                    conn, asset_id, asset_kind, content_hash, source_kind, generation_params,
                    declared_scope, portrait_subjects, actor.actor_id, ASSET_STATE_QUARANTINED,
                    now, prior_asset_ref=original["asset_id"],
                )
                self._emit(
                    conn, "ASSET_QUARANTINED", "media_asset", asset_id, now,
                    {
                        "content_hash": content_hash,
                        "prior_asset_ref": original["asset_id"],
                        "drift_fields": drift,
                        "declared_by": actor.actor_id,
                    },
                )
                return RegistrationResult(asset_id, ASSET_STATE_QUARANTINED, reused=False)
            asset_id = _new_id("asset")
            self._insert_asset(
                conn, asset_id, asset_kind, content_hash, source_kind, generation_params,
                declared_scope, portrait_subjects, actor.actor_id, ASSET_STATE_REGISTERED, now,
            )
            conn.execute(
                "INSERT INTO fingerprint_registry(content_hash, asset_id, source_kind, scope_key)"
                " VALUES (?, ?, ?, ?)",
                (content_hash, asset_id, source_kind, scope_key),
            )
            self._emit(
                conn, "ASSET_REGISTERED", "media_asset", asset_id, now,
                {
                    "content_hash": content_hash,
                    "source_kind": source_kind,
                    "asset_kind": asset_kind,
                    "declared_by": actor.actor_id,
                    "generation_params": dict(generation_params) if generation_params else None,
                    "declared_scope": dict(declared_scope) if declared_scope else None,
                    "portrait_subjects": list(portrait_subjects or []),
                },
            )
            return RegistrationResult(asset_id, ASSET_STATE_REGISTERED, reused=False)

    @staticmethod
    def _insert_asset(conn, asset_id, asset_kind, content_hash, source_kind, generation_params,
                      declared_scope, portrait_subjects, declared_by, state, now,
                      prior_asset_ref=None) -> None:
        conn.execute(
            "INSERT INTO assets(asset_id, asset_kind, content_hash, source_kind,"
            " generation_params, declared_scope, portrait_subjects, declared_by, state,"
            " prior_asset_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                asset_id,
                asset_kind,
                content_hash,
                source_kind,
                _canon(generation_params) if generation_params is not None else None,
                _canon(declared_scope) if declared_scope is not None else None,
                _canon(list(portrait_subjects)) if portrait_subjects else None,
                declared_by,
                state,
                prior_asset_ref,
                _iso(now),
            ),
        )

    def resolve_quarantine(self, actor: Actor, asset_id: str, decision: str, *, now: datetime) -> None:
        """隔离审查结论：cleared 转为可用，rejected 保持禁用。"""
        self._require_role(actor, ROLE_FACT_CHECKER)
        if decision not in ("cleared", "rejected"):
            raise ServiceError("隔离结论必须是 cleared 或 rejected")
        with self.store.transaction() as conn:
            asset = self._get_asset(conn, asset_id)
            if asset["state"] != ASSET_STATE_QUARANTINED:
                raise StateError("只有隔离中的素材可以解除审查")
            new_state = ASSET_STATE_REGISTERED if decision == "cleared" else ASSET_STATE_REJECTED
            conn.execute("UPDATE assets SET state = ? WHERE asset_id = ?", (new_state, asset_id))
            self._emit(
                conn, "QUARANTINE_RESOLVED", "media_asset", asset_id, now,
                {"resolved_by": actor.actor_id, "decision": decision},
            )

    # ------------------------------------------------------------------
    # 权利申报与核验：只能申报自己的创作，核验须由他人完成
    # ------------------------------------------------------------------
    def declare_rights(
        self,
        actor: Actor,
        asset_ref: str,
        grant_kind: str,
        grantor_ref: str,
        scope: Mapping[str, Any],
        *,
        now: datetime,
    ) -> str:
        """申报词曲或肖像授权；申报人必须是权利人本人。"""
        _iso(now)
        if actor.actor_id != grantor_ref:
            raise PermissionDenied("作者只能申报自己的创作，不能替他人确认权利")
        if grant_kind not in GRANT_KINDS:
            raise ServiceError(f"未知授权类型: {grant_kind}")
        self._validate_scope(scope)
        with self.store.transaction() as conn:
            self._get_asset(conn, asset_ref)
            grant_id = _new_id("grant")
            conn.execute(
                "INSERT INTO grants(grant_id, asset_ref, grant_kind, grantor_ref, declared_by,"
                " scope, state, verified_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    grant_id,
                    asset_ref,
                    grant_kind,
                    grantor_ref,
                    actor.actor_id,
                    _canon(dict(scope)),
                    GRANT_STATE_DECLARED,
                    _iso(now),
                ),
            )
            self._emit(
                conn, "RIGHTS_DECLARED", "rights_grant", grant_id, now,
                {
                    "grantor_ref": grantor_ref,
                    "scope": dict(scope),
                    "asset_ref": asset_ref,
                    "grant_kind": grant_kind,
                    "declared_by": actor.actor_id,
                },
            )
            return grant_id

    @staticmethod
    def _validate_scope(scope: Mapping[str, Any]) -> None:
        if not isinstance(scope, Mapping):
            raise ServiceError("授权范围必须是对象")
        channels = scope.get("channels")
        if not isinstance(channels, list) or not channels or not all(
            isinstance(channel, str) and channel for channel in channels
        ):
            raise ServiceError("授权范围必须包含非空 channels 列表")
        if not isinstance(scope.get("commercial"), bool):
            raise ServiceError("授权范围必须包含 commercial 布尔值")
        term_end = scope.get("term_end")
        if term_end is not None:
            parsed = _parse(term_end)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ServiceError("授权期限必须携带时区")

    def verify_rights(self, actor: Actor, grant_id: str, *, now: datetime) -> None:
        """事实核验角色确认授权；申报人不能自证。"""
        self._require_role(actor, ROLE_FACT_CHECKER)
        with self.store.transaction() as conn:
            grant = conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if grant is None:
                raise NotFoundError(f"授权不存在: {grant_id}")
            if grant["state"] != GRANT_STATE_DECLARED:
                raise StateError("授权已核验，不能重复确认")
            if grant["declared_by"] == actor.actor_id:
                raise PermissionDenied("申报人不能自行核验自己的权利申报")
            conn.execute(
                "UPDATE grants SET state = ?, verified_by = ? WHERE grant_id = ?",
                (GRANT_STATE_VERIFIED, actor.actor_id, grant_id),
            )
            self._emit(
                conn, "RIGHTS_VERIFIED", "rights_grant", grant_id, now,
                {"verified_by": actor.actor_id, "asset_ref": grant["asset_ref"]},
            )

    def record_contribution(
        self,
        actor: Actor,
        asset_ref: str,
        contribution_role: str,
        *,
        contributor_ref: str | None = None,
        now: datetime,
    ) -> None:
        """记录创作者贡献；只能申报自己的贡献，不能替他人申报。"""
        contributor = contributor_ref or actor.actor_id
        if contributor != actor.actor_id:
            raise PermissionDenied("不能替他人申报创作贡献")
        if contribution_role not in CONTRIBUTION_ROLES:
            raise ServiceError(f"未知贡献类型: {contribution_role}")
        with self.store.transaction() as conn:
            self._get_asset(conn, asset_ref)
            conn.execute(
                "INSERT INTO contributions(asset_ref, contributor_ref, contribution_role, declared_by)"
                " VALUES (?, ?, ?, ?)",
                (asset_ref, contributor, contribution_role, actor.actor_id),
            )
            self._emit(
                conn, "CONTRIBUTION_RECORDED", "media_asset", asset_ref, now,
                {
                    "contributor_ref": contributor,
                    "contribution_role": contribution_role,
                    "asset_ref": asset_ref,
                },
            )

    # ------------------------------------------------------------------
    # 剪辑版本与曲目原子锁
    # ------------------------------------------------------------------
    def create_revision(self, actor: Actor, items: Sequence[Mapping[str, Any]], *, now: datetime) -> str:
        """创建剪辑方案；条目为画面、声音或说明。"""
        with self.store.transaction() as conn:
            normalized = self._normalize_items(conn, items)
            revision_id = _new_id("rev")
            conn.execute(
                "INSERT INTO revisions(revision_id, state, items, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (revision_id, REVISION_STATE_DRAFT, _canon(normalized), actor.actor_id, _iso(now)),
            )
            self._emit(
                conn, "REVISION_CREATED", "edit_revision", revision_id, now,
                {"created_by": actor.actor_id, "items": normalized},
            )
            return revision_id

    def _normalize_items(self, conn, items: Sequence[Mapping[str, Any]]) -> list[dict]:
        if not items:
            raise ServiceError("剪辑版本至少需要一个条目")
        normalized = []
        for position, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise ServiceError("剪辑条目必须是对象")
            kind = item.get("kind")
            if kind not in ITEM_KINDS:
                raise ServiceError(f"未知条目类型: {kind}")
            if kind == ITEM_KIND_DESCRIPTION:
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ServiceError("说明条目必须包含非空文本")
                normalized.append({"position": position, "kind": kind, "text": text})
                continue
            asset_ref = item.get("asset_ref")
            if not isinstance(asset_ref, str) or not asset_ref:
                raise ServiceError("素材条目必须引用 asset_ref")
            asset = self._get_asset(conn, asset_ref)
            expected = ITEM_KIND_VIDEO if asset["asset_kind"] == ASSET_KIND_VIDEO else ITEM_KIND_AUDIO
            if kind != expected:
                raise ServiceError("条目类型与素材类别不一致")
            normalized.append({"position": position, "kind": kind, "asset_ref": asset_ref})
        return normalized

    def reserve_tracks(self, actor: Actor, revision_id: str, track_refs: Sequence[str], *, now: datetime) -> None:
        """原子锁定剪辑方案占用的曲目；任一冲突则整体失败，不留部分锁。"""
        if not track_refs:
            raise ServiceError("锁定曲目列表不能为空")
        if len(set(track_refs)) != len(track_refs):
            raise ServiceError("锁定曲目列表存在重复")
        with self.store.transaction() as conn:
            revision = self._get_revision(conn, revision_id)
            if revision["state"] != REVISION_STATE_DRAFT:
                raise StateError("只有草稿状态的剪辑方案可以锁定曲目")
            revision_tracks = {
                item["asset_ref"]
                for item in json.loads(revision["items"])
                if item["kind"] == ITEM_KIND_AUDIO
            }
            conflicts = []
            for track_ref in track_refs:
                if track_ref not in revision_tracks:
                    raise ServiceError(f"曲目不在剪辑方案中: {track_ref}")
                holder = conn.execute(
                    "SELECT revision_ref FROM track_locks WHERE track_ref = ?", (track_ref,)
                ).fetchone()
                if holder is None or holder["revision_ref"] == revision_id:
                    continue
                other = self._get_revision(conn, holder["revision_ref"])
                if other["state"] in REVISION_ACTIVE_STATES:
                    conflicts.append(track_ref)
                else:
                    conn.execute("DELETE FROM track_locks WHERE track_ref = ?", (track_ref,))
            if conflicts:
                raise ConflictError(f"曲目被其他剪辑方案占用: {sorted(conflicts)}")
            for track_ref in track_refs:
                conn.execute(
                    "INSERT OR IGNORE INTO track_locks(track_ref, revision_ref, acquired_at)"
                    " VALUES (?, ?, ?)",
                    (track_ref, revision_id, _iso(now)),
                )
            self._emit(
                conn, "TRACKS_RESERVED", "edit_revision", revision_id, now,
                {"revision_ref": revision_id, "track_refs": sorted(track_refs)},
            )

    def _release_locks(self, conn, revision_id: str, now: datetime) -> list[str]:
        rows = conn.execute(
            "SELECT track_ref FROM track_locks WHERE revision_ref = ?", (revision_id,)
        ).fetchall()
        if not rows:
            return []
        conn.execute("DELETE FROM track_locks WHERE revision_ref = ?", (revision_id,))
        track_refs = sorted(row["track_ref"] for row in rows)
        self._emit(
            conn, "TRACKS_RELEASED", "edit_revision", revision_id, now,
            {"revision_ref": revision_id, "track_refs": track_refs},
        )
        return track_refs

    def abandon_revision(self, actor: Actor, revision_id: str, *, now: datetime) -> None:
        """放弃剪辑方案并释放其曲目锁。"""
        with self.store.transaction() as conn:
            revision = self._get_revision(conn, revision_id)
            if revision["state"] not in REVISION_ACTIVE_STATES:
                raise StateError("只有未发布的剪辑方案可以放弃")
            conn.execute(
                "UPDATE revisions SET state = ? WHERE revision_id = ?",
                (REVISION_STATE_ABANDONED, revision_id),
            )
            self._emit(
                conn, "REVISION_ABANDONED", "edit_revision", revision_id, now,
                {"abandoned_by": actor.actor_id},
            )
            self._release_locks(conn, revision_id, now)

    def signoff(self, actor: Actor, revision_id: str, signoff_kind: str, *, now: datetime) -> None:
        """事实核验或商业发布签署；两类签署必须由不同人员完成。"""
        if signoff_kind == SIGNOFF_FACT:
            self._require_role(actor, ROLE_FACT_CHECKER)
        elif signoff_kind == SIGNOFF_COMMERCIAL:
            self._require_role(actor, ROLE_RELEASE_MANAGER)
        else:
            raise ServiceError(f"未知签署类型: {signoff_kind}")
        with self.store.transaction() as conn:
            revision = self._get_revision(conn, revision_id)
            if revision["state"] not in REVISION_ACTIVE_STATES:
                raise StateError("当前状态不允许签署")
            fact_signed_by = revision["fact_signed_by"]
            commercial_signed_by = revision["commercial_signed_by"]
            if signoff_kind == SIGNOFF_FACT:
                if fact_signed_by:
                    raise StateError("事实核验已签署")
                if commercial_signed_by == actor.actor_id:
                    raise PermissionDenied("事实核验与商业发布须由不同人员签署")
                fact_signed_by = actor.actor_id
            else:
                if commercial_signed_by:
                    raise StateError("商业发布已签署")
                if fact_signed_by == actor.actor_id:
                    raise PermissionDenied("事实核验与商业发布须由不同人员签署")
                commercial_signed_by = actor.actor_id
            ready = fact_signed_by and commercial_signed_by
            conn.execute(
                "UPDATE revisions SET fact_signed_by = ?, commercial_signed_by = ?, state = ?"
                " WHERE revision_id = ?",
                (
                    fact_signed_by,
                    commercial_signed_by,
                    REVISION_STATE_READY if ready else revision["state"],
                    revision_id,
                ),
            )
            self._emit(
                conn, "REVISION_SIGNOFF", "edit_revision", revision_id, now,
                {"signoff_kind": signoff_kind, "signed_by": actor.actor_id},
            )
            if ready:
                self._emit(
                    conn, "REVISION_FROZEN", "edit_revision", revision_id, now,
                    {
                        "items": json.loads(revision["items"]),
                        "fact_signed_by": fact_signed_by,
                        "commercial_signed_by": commercial_signed_by,
                    },
                )

    # ------------------------------------------------------------------
    # 渠道发布：冻结实际采用的画面、声音与说明
    # ------------------------------------------------------------------
    def post_release(
        self,
        actor: Actor,
        revision_id: str,
        channel: str,
        *,
        items_override: Sequence[Mapping[str, Any]] | None = None,
        notices: Sequence[Mapping[str, Any]] | None = None,
        now: datetime,
    ) -> str:
        """发布到渠道并冻结快照；校验双签署、素材状态、渠道限制与授权覆盖。"""
        self._require_role(actor, ROLE_RELEASE_MANAGER)
        _iso(now)
        policy = self._policy_for(channel)
        notice_list = self._validate_notices(notices)
        with self.store.transaction() as conn:
            revision = self._get_revision(conn, revision_id)
            if revision["state"] != REVISION_STATE_READY:
                raise StateError("剪辑版本未完成事实核验与商业发布双签署")
            revision_items = json.loads(revision["items"])
            if items_override is None:
                actual_items = revision_items
            else:
                actual_items = self._normalize_override(conn, revision_items, items_override)
            problems: list[str] = []
            frozen_items = []
            for item in actual_items:
                if item["kind"] == ITEM_KIND_DESCRIPTION:
                    frozen_items.append(
                        {"position": item["position"], "kind": item["kind"], "text": item["text"]}
                    )
                    continue
                asset = self._get_asset(conn, item["asset_ref"])
                problems.extend(self._asset_release_problems(conn, asset, policy, channel, now))
                holder = conn.execute(
                    "SELECT revision_ref FROM track_locks WHERE track_ref = ?", (asset["asset_id"],)
                ).fetchone()
                if holder is not None and holder["revision_ref"] != revision_id:
                    other = self._get_revision(conn, holder["revision_ref"])
                    if other["state"] in REVISION_ACTIVE_STATES:
                        problems.append(f"曲目被剪辑方案 {holder['revision_ref']} 占用")
                    else:
                        conn.execute("DELETE FROM track_locks WHERE track_ref = ?", (asset["asset_id"],))
                frozen_items.append(
                    {
                        "position": item["position"],
                        "kind": item["kind"],
                        "asset_ref": asset["asset_id"],
                        "content_hash": asset["content_hash"],
                        "source_kind": asset["source_kind"],
                    }
                )
            if problems:
                raise StateError("；".join(sorted(set(problems))))
            release_id = _new_id("rel")
            snapshot = {
                "revision_ref": revision_id,
                "channel": channel,
                "frozen_at": _iso(now),
                "items": frozen_items,
                "notices": notice_list,
            }
            conn.execute(
                "INSERT INTO releases(release_id, revision_ref, channel, state, frozen_snapshot,"
                " notices, posted_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (release_id, revision_id, channel, RELEASE_STATE_LIVE, _canon(snapshot),
                 _canon(notice_list), _iso(now)),
            )
            conn.execute(
                "UPDATE revisions SET state = ? WHERE revision_id = ?",
                (REVISION_STATE_RELEASED, revision_id),
            )
            self._release_locks(conn, revision_id, now)
            self._emit(
                conn, "RELEASE_POSTED", "channel_release", release_id, now,
                {
                    "channel": channel,
                    "revision_ref": revision_id,
                    "frozen_items": frozen_items,
                    "notices": notice_list,
                },
            )
            return release_id

    def _normalize_override(self, conn, revision_items, items_override) -> list[dict]:
        allowed_assets = {item["asset_ref"] for item in revision_items if "asset_ref" in item}
        normalized = []
        for position, item in enumerate(items_override):
            kind = item.get("kind")
            if kind == ITEM_KIND_DESCRIPTION:
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ServiceError("说明条目必须包含非空文本")
                normalized.append({"position": position, "kind": kind, "text": text})
                continue
            asset_ref = item.get("asset_ref")
            if asset_ref not in allowed_assets:
                raise ServiceError(f"渠道成片只能采用剪辑方案内的素材: {asset_ref}")
            asset = self._get_asset(conn, asset_ref)
            expected = ITEM_KIND_VIDEO if asset["asset_kind"] == ASSET_KIND_VIDEO else ITEM_KIND_AUDIO
            if kind != expected:
                raise ServiceError("条目类型与素材类别不一致")
            normalized.append({"position": position, "kind": kind, "asset_ref": asset_ref})
        if not normalized:
            raise ServiceError("渠道成片至少需要一个条目")
        return normalized

    @staticmethod
    def _validate_notices(notices) -> list[dict]:
        notice_list = []
        for notice in notices or []:
            if not isinstance(notice, Mapping) or not notice.get("kind"):
                raise ServiceError("限定条目必须包含 kind")
            notice_list.append(dict(notice))
        return notice_list

    def _asset_release_problems(self, conn, asset, policy: ChannelPolicy, channel: str,
                                now: datetime) -> list[str]:
        """发布门禁与许可解释共用的逐项校验；生成参数不参与权利判断。"""
        problems = []
        if asset["state"] != ASSET_STATE_REGISTERED:
            problems.append(f"素材 {asset['asset_id']} 状态为 {asset['state']}，不可用于发布")
            return problems
        if asset["asset_kind"] == ASSET_KIND_AUDIO:
            if asset["source_kind"] == SOURCE_AI_GENERATED and not policy.allow_ai_music:
                problems.append(f"渠道 {channel} 不允许 AI 生成音乐")
            if not self._has_covering_grant(
                conn, asset["asset_id"], GRANT_KIND_COMPOSITION, policy, channel, now
            ):
                problems.append(f"素材 {asset['asset_id']} 缺少覆盖渠道 {channel} 的词曲授权")
        if asset["asset_kind"] == ASSET_KIND_VIDEO and policy.requires_portrait_grant:
            subjects = json.loads(asset["portrait_subjects"] or "[]")
            for subject in subjects:
                if not self._has_covering_grant(
                    conn, asset["asset_id"], GRANT_KIND_PORTRAIT, policy, channel, now,
                    grantor=subject,
                ):
                    problems.append(f"缺少 {subject} 覆盖渠道 {channel} 的肖像授权")
        return problems

    def _has_covering_grant(self, conn, asset_ref: str, grant_kind: str, policy: ChannelPolicy,
                            channel: str, now: datetime, grantor: str | None = None) -> bool:
        sql = ("SELECT scope FROM grants WHERE asset_ref = ? AND grant_kind = ? AND state = ?")
        params: list[Any] = [asset_ref, grant_kind, GRANT_STATE_VERIFIED]
        if grantor is not None:
            sql += " AND grantor_ref = ?"
            params.append(grantor)
        rows = conn.execute(sql, params).fetchall()
        return any(
            self._scope_covers(json.loads(row["scope"]), channel, policy.commercial, now)
            for row in rows
        )

    @staticmethod
    def _scope_covers(scope: Mapping[str, Any], channel: str, commercial: bool, now: datetime) -> bool:
        channels = scope.get("channels") or []
        if "all" not in channels and channel not in channels:
            return False
        if commercial and not scope.get("commercial", False):
            return False
        term_end = scope.get("term_end")
        if term_end and _parse(term_end) <= now:
            return False
        return True

    def end_release(self, actor: Actor, release_id: str, *, now: datetime) -> None:
        """结束渠道活动；冻结快照保留作为当时证据。"""
        self._require_role(actor, ROLE_RELEASE_MANAGER)
        with self.store.transaction() as conn:
            release = self._get_release(conn, release_id)
            if release["state"] == RELEASE_STATE_ENDED:
                raise StateError("渠道活动已结束")
            conn.execute(
                "UPDATE releases SET state = ?, ended_at = ? WHERE release_id = ?",
                (RELEASE_STATE_ENDED, _iso(now), release_id),
            )
            self._emit(
                conn, "RELEASE_ENDED", "channel_release", release_id, now,
                {"ended_by": actor.actor_id},
            )

    # ------------------------------------------------------------------
    # 来源撤回、替换任务与下架
    # ------------------------------------------------------------------
    def withdraw_source(
        self,
        actor: Actor,
        asset_ref: str,
        reason: str,
        *,
        replacement_due_at: datetime | None = None,
        now: datetime,
    ) -> dict:
        """撤回素材来源。

        只阻止未发布的剪辑版本；对在线发布生成范围明确的替换任务；
        已结束的活动保留当时证据，不做任何改动。
        """
        self._require_role(actor, ROLE_RIGHTS_OFFICER)
        if not reason or not reason.strip():
            raise ServiceError("撤回必须说明原因")
        with self.store.transaction() as conn:
            asset = self._get_asset(conn, asset_ref)
            if asset["state"] != ASSET_STATE_REGISTERED:
                raise StateError(f"素材当前状态为 {asset['state']}，不能撤回")
            conn.execute(
                "UPDATE assets SET state = ? WHERE asset_id = ?",
                (ASSET_STATE_WITHDRAWN, asset_ref),
            )
            self._emit(
                conn, "SOURCE_WITHDRAWN", "media_asset", asset_ref, now,
                {"reason": reason, "withdrawn_by": actor.actor_id},
            )
            blocked = self._block_unpublished_revisions(conn, asset_ref, reason, now)
            tasks = []
            live_releases = conn.execute(
                "SELECT * FROM releases WHERE state = ?", (RELEASE_STATE_LIVE,)
            ).fetchall()
            for release in live_releases:
                snapshot = json.loads(release["frozen_snapshot"])
                used = [item for item in snapshot["items"] if item.get("asset_ref") == asset_ref]
                if not used:
                    continue
                task_id = _new_id("task")
                scope = {
                    "release_ref": release["release_id"],
                    "channel": release["channel"],
                    "items": used,
                }
                conn.execute(
                    "INSERT INTO replacement_tasks(task_id, release_ref, asset_ref, channel,"
                    " scope, state, reason, due_at, resolution, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        task_id,
                        release["release_id"],
                        asset_ref,
                        release["channel"],
                        _canon(scope),
                        TASK_STATE_OPEN,
                        reason,
                        _iso(replacement_due_at) if replacement_due_at else None,
                        _iso(now),
                    ),
                )
                self._emit(
                    conn, "REPLACEMENT_TASK_OPENED", "replacement_task", task_id, now,
                    {
                        "release_ref": release["release_id"],
                        "asset_ref": asset_ref,
                        "channel": release["channel"],
                        "scope": scope,
                        "due_at": _iso(replacement_due_at) if replacement_due_at else None,
                    },
                )
                if replacement_due_at is not None:
                    self._enqueue(conn, JOB_ENFORCE_REPLACEMENT, {"task_ref": task_id}, replacement_due_at)
                tasks.append(task_id)
            return {"blocked_revisions": blocked, "replacement_tasks": tasks}

    def _block_unpublished_revisions(self, conn, asset_ref: str, reason: str, now: datetime) -> list[str]:
        blocked = []
        candidates = conn.execute(
            "SELECT * FROM revisions WHERE state IN (?, ?)",
            (REVISION_STATE_DRAFT, REVISION_STATE_READY),
        ).fetchall()
        for revision in candidates:
            items = json.loads(revision["items"])
            if not any(item.get("asset_ref") == asset_ref for item in items):
                continue
            conn.execute(
                "UPDATE revisions SET state = ?, blocked_reason = ? WHERE revision_id = ?",
                (REVISION_STATE_BLOCKED, reason, revision["revision_id"]),
            )
            self._emit(
                conn, "REVISION_BLOCKED", "edit_revision", revision["revision_id"], now,
                {"asset_ref": asset_ref, "reason": reason},
            )
            self._release_locks(conn, revision["revision_id"], now)
            blocked.append(revision["revision_id"])
        return blocked

    def close_replacement_task(self, actor: Actor, task_id: str, resolution: Mapping[str, Any], *, now: datetime) -> None:
        """完成替换任务，记录处理结果（例如替换后的新发布）。"""
        self._require_role(actor, ROLE_RELEASE_MANAGER)
        if not isinstance(resolution, Mapping) or not resolution:
            raise ServiceError("替换任务关闭必须记录处理结果")
        with self.store.transaction() as conn:
            task = conn.execute(
                "SELECT * FROM replacement_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"替换任务不存在: {task_id}")
            if task["state"] != TASK_STATE_OPEN:
                raise StateError("替换任务已关闭")
            conn.execute(
                "UPDATE replacement_tasks SET state = ?, resolution = ? WHERE task_id = ?",
                (TASK_STATE_DONE, _canon(dict(resolution)), task_id),
            )
            self._emit(
                conn, "REPLACEMENT_TASK_CLOSED", "replacement_task", task_id, now,
                {"closed_by": actor.actor_id, "resolution": dict(resolution)},
            )

    def open_takedown(
        self,
        actor: Actor,
        target_kind: str,
        target_ref: str,
        reason: str,
        due_at: datetime,
        *,
        now: datetime,
    ) -> str:
        """登记下架通知并挂出期限执行任务；到期由后台 worker 处理。"""
        self._require_role(actor, ROLE_RIGHTS_OFFICER)
        if target_kind not in ("asset", "release"):
            raise ServiceError("下架对象必须是 asset 或 release")
        if not reason or not reason.strip():
            raise ServiceError("下架必须说明原因")
        _iso(due_at)
        with self.store.transaction() as conn:
            if target_kind == "asset":
                self._get_asset(conn, target_ref)
            else:
                self._get_release(conn, target_ref)
            takedown_id = _new_id("td")
            conn.execute(
                "INSERT INTO takedowns(takedown_id, target_kind, target_ref, reason, state,"
                " due_at, opened_by, opened_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                (takedown_id, target_kind, target_ref, reason, _iso(due_at), actor.actor_id, _iso(now)),
            )
            self._emit(
                conn, "TAKEDOWN_OPENED", "takedown_notice", takedown_id, now,
                {
                    "target_kind": target_kind,
                    "target_ref": target_ref,
                    "reason": reason,
                    "due_at": _iso(due_at),
                    "opened_by": actor.actor_id,
                },
            )
            self._enqueue(conn, JOB_TAKEDOWN_ENFORCE, {"takedown_ref": takedown_id}, due_at)
            return takedown_id

    # ------------------------------------------------------------------
    # 更正与传播
    # ------------------------------------------------------------------
    def issue_correction(self, actor: Actor, release_id: str, correction: Mapping[str, Any], *, now: datetime) -> str:
        """对已发布内容登记更正；在线发布会生成渠道传播任务。"""
        self._require_role(actor, ROLE_FACT_CHECKER)
        if not isinstance(correction, Mapping) or not correction.get("field"):
            raise ServiceError("更正必须说明字段")
        with self.store.transaction() as conn:
            release = self._get_release(conn, release_id)
            correction_id = _new_id("corr")
            conn.execute(
                "INSERT INTO corrections(correction_id, release_ref, correction, issued_by, issued_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (correction_id, release_id, _canon(dict(correction)), actor.actor_id, _iso(now)),
            )
            self._emit(
                conn, "CORRECTION_ISSUED", "channel_release", release_id, now,
                {"release_ref": release_id, "correction": dict(correction), "correction_ref": correction_id},
            )
            if release["state"] == RELEASE_STATE_LIVE:
                self._enqueue(conn, JOB_CORRECTION_PROPAGATION, {"correction_ref": correction_id}, now)
            return correction_id

    # ------------------------------------------------------------------
    # 解释接口
    # ------------------------------------------------------------------
    def explain_release(self, release_id: str, *, now: datetime) -> dict:
        """从一段成片解释素材来源与许可状态；已结束活动同样返回当时证据。"""
        _iso(now)
        release = self.store.read_one("SELECT * FROM releases WHERE release_id = ?", (release_id,))
        if release is None:
            raise NotFoundError(f"渠道发布不存在: {release_id}")
        snapshot = json.loads(release["frozen_snapshot"])
        policy = self._policy_for(release["channel"])
        items_out = []
        problems: list[str] = []
        with self.store.transaction() as conn:
            for item in snapshot["items"]:
                if item["kind"] == ITEM_KIND_DESCRIPTION:
                    items_out.append(
                        {"position": item["position"], "kind": item["kind"], "text": item["text"]}
                    )
                    continue
                asset = self._get_asset(conn, item["asset_ref"])
                grants = conn.execute(
                    "SELECT grant_kind, grantor_ref, scope, state, verified_by FROM grants"
                    " WHERE asset_ref = ? ORDER BY grant_id",
                    (asset["asset_id"],),
                ).fetchall()
                contributors = conn.execute(
                    "SELECT contributor_ref, contribution_role FROM contributions"
                    " WHERE asset_ref = ? ORDER BY contribution_id",
                    (asset["asset_id"],),
                ).fetchall()
                item_problems = self._asset_release_problems(
                    conn, asset, policy, release["channel"], now
                )
                problems.extend(item_problems)
                entry = {
                    "position": item["position"],
                    "kind": item["kind"],
                    "asset_ref": asset["asset_id"],
                    "content_hash": item["content_hash"],
                    "source_kind": item["source_kind"],
                    "asset_state": asset["state"],
                    "rights": [
                        {
                            "grant_kind": grant["grant_kind"],
                            "grantor_ref": grant["grantor_ref"],
                            "scope": json.loads(grant["scope"]),
                            "state": grant["state"],
                            "verified_by": grant["verified_by"],
                        }
                        for grant in grants
                    ],
                    "contributors": [
                        {"contributor_ref": c["contributor_ref"], "role": c["contribution_role"]}
                        for c in contributors
                    ],
                    "issues": item_problems,
                }
                if item["source_kind"] == SOURCE_AI_GENERATED:
                    entry["generation_params"] = (
                        json.loads(asset["generation_params"]) if asset["generation_params"] else None
                    )
                    entry["generation_params_rights_proof"] = False
                    entry["note"] = "生成参数仅为谱系记录，不构成版权证明"
                items_out.append(entry)
        open_tasks = [
            {
                "task_id": row["task_id"],
                "asset_ref": row["asset_ref"],
                "scope": json.loads(row["scope"]),
                "due_at": row["due_at"],
            }
            for row in self.store.read(
                "SELECT * FROM replacement_tasks WHERE release_ref = ? AND state = ?",
                (release_id, TASK_STATE_OPEN),
            )
        ]
        if open_tasks:
            problems.append("存在未完成的来源替换任务")
        return {
            "release_id": release_id,
            "revision_ref": release["revision_ref"],
            "channel": release["channel"],
            "state": release["state"],
            "posted_at": release["posted_at"],
            "ended_at": release["ended_at"],
            "items": items_out,
            "notices": snapshot["notices"],
            "license_status": "issues" if problems else "clear",
            "issues": sorted(set(problems)),
            "open_replacement_tasks": open_tasks,
        }

    def explain_asset(self, asset_id: str) -> dict:
        """单个素材的谱系：登记、授权、贡献与状态流转事件。"""
        asset = self.store.read_one("SELECT * FROM assets WHERE asset_id = ?", (asset_id,))
        if asset is None:
            raise NotFoundError(f"素材不存在: {asset_id}")
        events = [
            {
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "version": row["version"],
                "payload": json.loads(row["payload"]),
            }
            for row in self.store.read(
                "SELECT * FROM events WHERE aggregate_type = 'media_asset' AND aggregate_id = ?"
                " ORDER BY version",
                (asset_id,),
            )
        ]
        grants = [
            {
                "grant_id": row["grant_id"],
                "grant_kind": row["grant_kind"],
                "grantor_ref": row["grantor_ref"],
                "scope": json.loads(row["scope"]),
                "state": row["state"],
                "verified_by": row["verified_by"],
            }
            for row in self.store.read(
                "SELECT * FROM grants WHERE asset_ref = ? ORDER BY grant_id", (asset_id,)
            )
        ]
        result = {
            "asset_id": asset_id,
            "asset_kind": asset["asset_kind"],
            "content_hash": asset["content_hash"],
            "source_kind": asset["source_kind"],
            "state": asset["state"],
            "declared_by": asset["declared_by"],
            "prior_asset_ref": asset["prior_asset_ref"],
            "grants": grants,
            "events": events,
        }
        if asset["source_kind"] == SOURCE_AI_GENERATED:
            result["generation_params"] = (
                json.loads(asset["generation_params"]) if asset["generation_params"] else None
            )
            result["generation_params_rights_proof"] = False
            result["note"] = "生成参数仅为谱系记录，不构成版权证明"
        return result

    def list_events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[dict]:
        """按聚合读取事件流，供审计与测试校验契约。"""
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if aggregate_type is not None:
            sql += " WHERE aggregate_type = ?"
            params.append(aggregate_type)
            if aggregate_id is not None:
                sql += " AND aggregate_id = ?"
                params.append(aggregate_id)
        sql += " ORDER BY occurred_at, event_id"
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "occurred_at": row["occurred_at"],
                "version": row["version"],
                "payload": json.loads(row["payload"]),
            }
            for row in self.store.read(sql, tuple(params))
        ]
