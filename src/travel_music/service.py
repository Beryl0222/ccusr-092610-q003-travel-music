"""素材谱系服务：在领域契约之上实现业务幂等、冲突隔离与状态推进。

覆盖的规则：
- 重复提交相同素材指纹沿用原决定；指纹相同但来源或授权范围变化时隔离审查。
- 作者只能申报自己的创作，不能替他人确认权利。
- 事实核验与商业发布须由不同角色、不同人员签署。
- 渠道发布时冻结实际采用的画面、声音和说明。
- 来源撤回只阻止未发布版本，对在线版本生成范围明确的替换任务；已结束活动保留当时证据。
- 多个剪辑方案占用同一首曲目时原子锁定。
- 生成参数摘要仅用于溯源，不构成版权证明。
- 下架期限与更正传播由持久化后台任务处理，中断后可继续。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import validate_event
from .models import (
    AssetKind,
    AssetStatus,
    GrantKind,
    GrantStatus,
    JobStatus,
    LineageError,
    LockConflict,
    NotFound,
    PermissionDenied,
    ReleaseStatus,
    RevisionStatus,
    SourceKind,
    StateConflict,
    TaskStatus,
)
from .store import Store

GENERATION_PARAMS_NOTE = "生成参数摘要仅用于溯源，不构成版权证明"

ROLE_FACT_CHECKER = "fact_checker"
ROLE_COMMERCIAL_APPROVER = "commercial_approver"
ROLE_RIGHTS_OFFICER = "rights_officer"

_SIGN_KINDS = ("fact", "commercial")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _parse_moment(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{field} 必须是合法的时间字符串") from None
    else:
        raise ValueError(f"{field} 必须是合法的时间字符串")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须携带时区")
    return parsed


class LineageService:
    def __init__(self, store: Store, schema: Mapping[str, Any]) -> None:
        self.store = store
        self.schema = schema

    # ------------------------------------------------------------------
    # 事件日志与幂等
    # ------------------------------------------------------------------
    def _seen(self, event_id: str) -> bool:
        return self.store.one("SELECT 1 FROM events WHERE event_id=?", (event_id,)) is not None

    def _replay(self, event_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM events WHERE event_id=?", (event_id,))
        return {
            "decision": "replayed",
            "event_id": event_id,
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
        }

    def _emit(
        self,
        *,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> None:
        row = self.store.one(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM events WHERE aggregate_type=? AND aggregate_id=?",
            (aggregate_type, aggregate_id),
        )
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": row["v"],
            "payload": dict(payload),
        }
        issues = validate_event(envelope, self.schema)
        if issues:
            detail = "; ".join(f"{issue.field}:{issue.code}" for issue in issues)
            raise StateConflict(f"服务生成的事件未通过契约校验: {detail}")
        self.store.execute(
            "INSERT INTO events(event_id, event_type, aggregate_type, aggregate_id, version, occurred_at, payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                event_type,
                aggregate_type,
                aggregate_id,
                envelope["version"],
                occurred_at,
                _canonical(payload),
            ),
        )

    def list_events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if aggregate_type is not None:
            sql += " WHERE aggregate_type=?"
            params.append(aggregate_type)
            if aggregate_id is not None:
                sql += " AND aggregate_id=?"
                params.append(aggregate_id)
        sql += " ORDER BY rowid"
        return [
            {**{k: row[k] for k in row.keys() if k != "payload"}, "payload": json.loads(row["payload"])}
            for row in self.store.all(sql, tuple(params))
        ]

    # ------------------------------------------------------------------
    # 素材登记与指纹决定
    # ------------------------------------------------------------------
    @staticmethod
    def _default_required_grants(kind: AssetKind, persons_depicted: bool) -> list[str]:
        if kind is AssetKind.MUSIC:
            return [GrantKind.COMPOSITION.value]
        if kind is AssetKind.FOOTAGE and persons_depicted:
            return [GrantKind.PORTRAIT.value]
        return []

    def register_asset(
        self,
        *,
        event_id: str,
        asset_id: str,
        kind: str,
        content_hash: str,
        source_kind: str,
        declared_by: str,
        occurred_at: str,
        generation_params: Mapping[str, Any] | None = None,
        license_scope: Mapping[str, Any] | None = None,
        required_grants: Sequence[str] | None = None,
        persons_depicted: bool = False,
    ) -> dict[str, Any]:
        kind_enum = AssetKind(kind)
        source_enum = SourceKind(source_kind)
        _parse_moment(occurred_at, "occurred_at")
        grants_required = (
            [GrantKind(g).value for g in required_grants]
            if required_grants is not None
            else self._default_required_grants(kind_enum, persons_depicted)
        )
        scope_key = _canonical(license_scope or {})
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            if self.store.one("SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)):
                raise StateConflict(f"素材标识 {asset_id} 已存在")
            holder = self.store.one("SELECT * FROM fingerprint_index WHERE content_hash=?", (content_hash,))
            if holder and holder["source_kind"] == source_enum.value and holder["scope_key"] == scope_key:
                # 重复提交相同素材指纹：沿用原决定。
                self._emit(
                    event_id=event_id,
                    event_type="ASSET_REGISTERED",
                    aggregate_type="media_asset",
                    aggregate_id=holder["asset_id"],
                    occurred_at=occurred_at,
                    payload={
                        "content_hash": content_hash,
                        "source_kind": source_enum.value,
                        "reused_decision_of": holder["asset_id"],
                    },
                )
                asset = self.store.one("SELECT status FROM assets WHERE asset_id=?", (holder["asset_id"],))
                return {"decision": "reused", "asset_id": holder["asset_id"], "status": asset["status"]}
            quarantined = holder is not None
            status = AssetStatus.QUARANTINED if quarantined else AssetStatus.ACTIVE
            self.store.execute(
                "INSERT INTO assets(asset_id, kind, content_hash, source_kind, generation_params, license_scope,"
                " required_grants, status, declared_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    kind_enum.value,
                    content_hash,
                    source_enum.value,
                    _canonical(generation_params) if generation_params is not None else None,
                    _canonical(license_scope) if license_scope is not None else None,
                    _canonical(grants_required),
                    status.value,
                    declared_by,
                    occurred_at,
                ),
            )
            if quarantined:
                # 指纹相同但来源或授权范围变化：隔离审查。
                task_id = f"quarantine-{event_id}"
                self.store.execute(
                    "INSERT INTO tasks(task_id, kind, status, scope, created_at) VALUES(?,?,?,?,?)",
                    (
                        task_id,
                        "quarantine_review",
                        TaskStatus.OPEN.value,
                        _canonical(
                            {
                                "asset_id": asset_id,
                                "holder_asset_id": holder["asset_id"],
                                "content_hash": content_hash,
                                "reason": "指纹相同但来源或授权范围变化",
                            }
                        ),
                        occurred_at,
                    ),
                )
            else:
                self.store.execute(
                    "INSERT INTO fingerprint_index(content_hash, asset_id, source_kind, scope_key) VALUES(?,?,?,?)",
                    (content_hash, asset_id, source_enum.value, scope_key),
                )
            self._emit(
                event_id=event_id,
                event_type="ASSET_REGISTERED",
                aggregate_type="media_asset",
                aggregate_id=asset_id,
                occurred_at=occurred_at,
                payload={
                    "content_hash": content_hash,
                    "source_kind": source_enum.value,
                    "decision": "quarantined" if quarantined else "registered",
                },
            )
            return {
                "decision": "quarantined" if quarantined else "registered",
                "asset_id": asset_id,
                "status": status.value,
            }

    # ------------------------------------------------------------------
    # 贡献申报与权利确认
    # ------------------------------------------------------------------
    def _asset_or_raise(self, asset_id: str):
        row = self.store.one("SELECT * FROM assets WHERE asset_id=?", (asset_id,))
        if row is None:
            raise NotFound(f"素材 {asset_id} 不存在")
        return row

    def declare_contribution(
        self,
        *,
        event_id: str,
        contribution_id: str,
        creator_id: str,
        asset_id: str,
        role: str,
        declared_by: str,
        occurred_at: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            if declared_by != creator_id:
                raise PermissionDenied("作者只能申报自己的创作，不能替他人申报")
            self._asset_or_raise(asset_id)
            self.store.execute(
                "INSERT INTO contributions(contribution_id, creator_id, asset_id, role, note, declared_by)"
                " VALUES(?,?,?,?,?,?)",
                (contribution_id, creator_id, asset_id, role, note, declared_by),
            )
            self._emit(
                event_id=event_id,
                event_type="CONTRIBUTION_DECLARED",
                aggregate_type="contribution",
                aggregate_id=contribution_id,
                occurred_at=occurred_at,
                payload={"creator_id": creator_id, "role": role, "asset_ref": asset_id},
            )
            return {"contribution_id": contribution_id, "status": "declared"}

    def declare_grant(
        self,
        *,
        event_id: str,
        grant_id: str,
        asset_id: str,
        grant_kind: str,
        grantor_ref: str,
        scope: Mapping[str, Any],
        declared_by: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        kind_enum = GrantKind(grant_kind)
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            self._asset_or_raise(asset_id)
            self.store.execute(
                "INSERT INTO grants(grant_id, asset_id, grant_kind, grantor_ref, scope, status, declared_by)"
                " VALUES(?,?,?,?,?,?,?)",
                (grant_id, asset_id, kind_enum.value, grantor_ref, _canonical(dict(scope)), GrantStatus.DECLARED.value, declared_by),
            )
            self._emit(
                event_id=event_id,
                event_type="RIGHTS_DECLARED",
                aggregate_type="rights_grant",
                aggregate_id=grant_id,
                occurred_at=occurred_at,
                payload={"grantor_ref": grantor_ref, "scope": dict(scope), "asset_ref": asset_id},
            )
            return {"grant_id": grant_id, "status": GrantStatus.DECLARED.value}

    def confirm_grant(
        self,
        *,
        event_id: str,
        grant_id: str,
        confirmed_by: str,
        confirmer_roles: Sequence[str],
        occurred_at: str,
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            grant = self.store.one("SELECT * FROM grants WHERE grant_id=?", (grant_id,))
            if grant is None:
                raise NotFound(f"授权 {grant_id} 不存在")
            if grant["status"] != GrantStatus.DECLARED.value:
                raise StateConflict(f"授权 {grant_id} 当前状态为 {grant['status']}，不能确认")
            is_grantor = confirmed_by == grant["grantor_ref"]
            is_officer = ROLE_RIGHTS_OFFICER in confirmer_roles
            if not (is_grantor or is_officer):
                raise PermissionDenied("只能由权利人本人或授权专员确认权利，作者不能替他人确认")
            self.store.execute(
                "UPDATE grants SET status=?, confirmed_by=? WHERE grant_id=?",
                (GrantStatus.CONFIRMED.value, confirmed_by, grant_id),
            )
            self._emit(
                event_id=event_id,
                event_type="RIGHTS_CONFIRMED",
                aggregate_type="rights_grant",
                aggregate_id=grant_id,
                occurred_at=occurred_at,
                payload={"grant_ref": grant_id, "confirmed_by": confirmed_by},
            )
            return {"grant_id": grant_id, "status": GrantStatus.CONFIRMED.value}

    # ------------------------------------------------------------------
    # 剪辑版本与签署
    # ------------------------------------------------------------------
    def create_revision(
        self,
        *,
        event_id: str,
        revision_id: str,
        items: Sequence[Mapping[str, Any]],
        created_by: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            norm_items = []
            for item in items:
                asset = self._asset_or_raise(str(item["asset_id"]))
                if asset["status"] == AssetStatus.WITHDRAWN.value:
                    raise StateConflict(f"素材 {item['asset_id']} 已撤回，不能进入新剪辑")
                norm_items.append(
                    {
                        "asset_id": asset["asset_id"],
                        "usage": item.get("usage"),
                        "segment": item.get("segment"),
                        "note": item.get("note"),
                    }
                )
            self.store.execute(
                "INSERT INTO revisions(revision_id, items, status, created_by) VALUES(?,?,?,?)",
                (revision_id, _canonical(norm_items), RevisionStatus.DRAFT.value, created_by),
            )
            self._emit(
                event_id=event_id,
                event_type="REVISION_CREATED",
                aggregate_type="edit_revision",
                aggregate_id=revision_id,
                occurred_at=occurred_at,
                payload={"items": norm_items, "created_by": created_by},
            )
            return {"revision_id": revision_id, "status": RevisionStatus.DRAFT.value}

    def _revision_or_raise(self, revision_id: str):
        row = self.store.one("SELECT * FROM revisions WHERE revision_id=?", (revision_id,))
        if row is None:
            raise NotFound(f"剪辑版本 {revision_id} 不存在")
        return row

    def sign_revision(
        self,
        *,
        event_id: str,
        revision_id: str,
        signer: str,
        signer_roles: Sequence[str],
        sign_kind: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        if sign_kind not in _SIGN_KINDS:
            raise ValueError(f"sign_kind 必须是 {_SIGN_KINDS} 之一")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            rev = self._revision_or_raise(revision_id)
            if rev["status"] == RevisionStatus.BLOCKED.value:
                raise StateConflict(f"剪辑版本 {revision_id} 已被阻止，不能签署")
            if sign_kind == "fact":
                if ROLE_FACT_CHECKER not in signer_roles:
                    raise PermissionDenied("事实核验须由事实核验角色签署")
                if rev["commercial_signed_by"] == signer:
                    raise PermissionDenied("事实核验与商业发布不能由同一人签署")
                self.store.execute("UPDATE revisions SET fact_signed_by=? WHERE revision_id=?", (signer, revision_id))
            else:
                if ROLE_COMMERCIAL_APPROVER not in signer_roles:
                    raise PermissionDenied("商业发布须由商业发布角色签署")
                if rev["fact_signed_by"] == signer:
                    raise PermissionDenied("事实核验与商业发布不能由同一人签署")
                self.store.execute(
                    "UPDATE revisions SET commercial_signed_by=? WHERE revision_id=?", (signer, revision_id)
                )
            updated = self._revision_or_raise(revision_id)
            approved = updated["fact_signed_by"] and updated["commercial_signed_by"]
            if approved and updated["status"] != RevisionStatus.APPROVED.value:
                self.store.execute(
                    "UPDATE revisions SET status=? WHERE revision_id=?", (RevisionStatus.APPROVED.value, revision_id)
                )
            self._emit(
                event_id=event_id,
                event_type="REVISION_SIGNED",
                aggregate_type="edit_revision",
                aggregate_id=revision_id,
                occurred_at=occurred_at,
                payload={"sign_kind": sign_kind, "signer": signer},
            )
            status = RevisionStatus.APPROVED.value if approved else updated["status"]
            return {"revision_id": revision_id, "status": status}

    # ------------------------------------------------------------------
    # 曲目原子锁
    # ------------------------------------------------------------------
    def acquire_track_lock(
        self, *, event_id: str, revision_id: str, track_asset_id: str, occurred_at: str
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            rev = self._revision_or_raise(revision_id)
            if rev["status"] == RevisionStatus.BLOCKED.value:
                raise StateConflict(f"剪辑版本 {revision_id} 已被阻止，不能占用曲目")
            track = self._asset_or_raise(track_asset_id)
            if track["kind"] != AssetKind.MUSIC.value:
                raise StateConflict(f"素材 {track_asset_id} 不是曲目，不能锁定")
            try:
                self.store.execute(
                    "INSERT INTO track_locks(track_asset_id, revision_id, acquired_at) VALUES(?,?,?)",
                    (track_asset_id, revision_id, occurred_at),
                )
            except sqlite3.IntegrityError:
                holder = self.store.one(
                    "SELECT revision_id FROM track_locks WHERE track_asset_id=?", (track_asset_id,)
                )
                raise LockConflict(f"曲目 {track_asset_id} 已被剪辑方案 {holder['revision_id']} 占用") from None
            self._emit(
                event_id=event_id,
                event_type="TRACK_LOCKED",
                aggregate_type="track_lock",
                aggregate_id=track_asset_id,
                occurred_at=occurred_at,
                payload={"revision_ref": revision_id, "track_ref": track_asset_id},
            )
            return {"track_asset_id": track_asset_id, "locked_by": revision_id}

    def release_track_lock(self, *, revision_id: str, track_asset_id: str) -> dict[str, Any]:
        with self.store.atomic():
            cursor = self.store.execute(
                "DELETE FROM track_locks WHERE track_asset_id=? AND revision_id=?", (track_asset_id, revision_id)
            )
            if cursor.rowcount == 0:
                raise NotFound(f"剪辑方案 {revision_id} 并未持有曲目 {track_asset_id} 的锁")
            return {"track_asset_id": track_asset_id, "released_by": revision_id}

    # ------------------------------------------------------------------
    # 许可状态与渠道发布
    # ------------------------------------------------------------------
    @staticmethod
    def _grant_covers(scope: Mapping[str, Any], channel: str | None) -> bool:
        if channel is None:
            return True
        channels = scope.get("channels")
        return channels is None or channel in channels or "*" in channels

    def _license_status(self, asset, channel: str | None) -> dict[str, Any]:
        required = json.loads(asset["required_grants"])
        grants = self.store.all("SELECT * FROM grants WHERE asset_id=?", (asset["asset_id"],))
        details = []
        overall = "clear"
        for kind in required:
            confirmed = [
                g
                for g in grants
                if g["grant_kind"] == kind
                and g["status"] == GrantStatus.CONFIRMED.value
                and self._grant_covers(json.loads(g["scope"]), channel)
            ]
            if confirmed:
                details.append({"grant_kind": kind, "status": "confirmed", "grant_id": confirmed[0]["grant_id"]})
                continue
            declared = [g for g in grants if g["grant_kind"] == kind and g["status"] == GrantStatus.DECLARED.value]
            if declared:
                details.append({"grant_kind": kind, "status": "pending_confirmation", "grant_id": declared[0]["grant_id"]})
                overall = "pending_confirmation"
            else:
                details.append({"grant_kind": kind, "status": "missing"})
                overall = "missing_grant"
        if asset["status"] == AssetStatus.QUARANTINED.value:
            overall = "quarantined"
        elif asset["status"] == AssetStatus.WITHDRAWN.value:
            overall = "withdrawn"
        return {"status": overall, "required_grants": required, "grants": details}

    def post_release(
        self,
        *,
        event_id: str,
        release_id: str,
        revision_id: str,
        channel: str,
        posted_by: str,
        occurred_at: str,
        campaign_id: str | None = None,
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            rev = self._revision_or_raise(revision_id)
            if rev["status"] != RevisionStatus.APPROVED.value:
                raise StateConflict("剪辑版本须完成事实核验与商业发布双签署后才能发布")
            if campaign_id is not None:
                campaign = self.store.one("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,))
                if campaign is None:
                    self.store.execute(
                        "INSERT INTO campaigns(campaign_id, status) VALUES(?,?)", (campaign_id, "open")
                    )
                elif campaign["status"] != "open":
                    raise StateConflict(f"活动 {campaign_id} 已结束，不能继续发布")
            problems = []
            frozen_items = []
            for item in json.loads(rev["items"]):
                asset = self._asset_or_raise(item["asset_id"])
                if asset["status"] != AssetStatus.ACTIVE.value:
                    problems.append(f"素材 {asset['asset_id']} 状态为 {asset['status']}")
                    continue
                license_status = self._license_status(asset, channel)
                if license_status["status"] != "clear":
                    problems.append(f"素材 {asset['asset_id']} 许可状态为 {license_status['status']}")
                frozen_items.append(
                    {
                        **item,
                        "kind": asset["kind"],
                        "source_kind": asset["source_kind"],
                        "content_hash": asset["content_hash"],
                        "license": license_status,
                    }
                )
            if problems:
                raise StateConflict("；".join(problems))
            snapshot = {
                "revision_id": revision_id,
                "channel": channel,
                "items": frozen_items,
                "fact_signed_by": rev["fact_signed_by"],
                "commercial_signed_by": rev["commercial_signed_by"],
                "frozen_at": occurred_at,
            }
            self.store.execute(
                "INSERT INTO releases(release_id, revision_id, channel, snapshot, status, posted_by, posted_at, campaign_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    revision_id,
                    channel,
                    _canonical(snapshot),
                    ReleaseStatus.LIVE.value,
                    posted_by,
                    occurred_at,
                    campaign_id,
                ),
            )
            self._emit(
                event_id=event_id,
                event_type="REVISION_FROZEN",
                aggregate_type="edit_revision",
                aggregate_id=revision_id,
                occurred_at=occurred_at,
                payload={"frozen_items": frozen_items, "channel": channel},
            )
            self._emit(
                event_id=f"{event_id}-posted",
                event_type="RELEASE_POSTED",
                aggregate_type="channel_release",
                aggregate_id=release_id,
                occurred_at=occurred_at,
                payload={"channel": channel, "revision_ref": revision_id},
            )
            return {"release_id": release_id, "status": ReleaseStatus.LIVE.value}

    # ------------------------------------------------------------------
    # 来源撤回、替换任务与活动证据
    # ------------------------------------------------------------------
    def withdraw_source(
        self,
        *,
        event_id: str,
        asset_id: str,
        reason: str,
        deadline_at: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        _parse_moment(deadline_at, "deadline_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            asset = self._asset_or_raise(asset_id)
            if asset["status"] == AssetStatus.WITHDRAWN.value:
                return {"asset_id": asset_id, "status": AssetStatus.WITHDRAWN.value, "already_withdrawn": True}
            self.store.execute(
                "UPDATE assets SET status=? WHERE asset_id=?", (AssetStatus.WITHDRAWN.value, asset_id)
            )
            self._emit(
                event_id=event_id,
                event_type="SOURCE_WITHDRAWN",
                aggregate_type="media_asset",
                aggregate_id=asset_id,
                occurred_at=occurred_at,
                payload={"reason": reason},
            )
            takedown_id = f"takedown-{event_id}"
            self.store.execute(
                "INSERT INTO takedowns(takedown_id, asset_id, reason, status, opened_at, deadline_at)"
                " VALUES(?,?,?,?,?,?)",
                (takedown_id, asset_id, reason, "open", occurred_at, deadline_at),
            )
            self._emit(
                event_id=f"{event_id}-takedown",
                event_type="TAKEDOWN_OPENED",
                aggregate_type="takedown_notice",
                aggregate_id=takedown_id,
                occurred_at=occurred_at,
                payload={"asset_ref": asset_id, "reason": reason, "deadline_at": deadline_at},
            )
            # 只阻止未发布版本。
            blocked = []
            for rev in self.store.all(
                "SELECT * FROM revisions WHERE status != ?", (RevisionStatus.BLOCKED.value,)
            ):
                items = json.loads(rev["items"])
                if not any(item["asset_id"] == asset_id for item in items):
                    continue
                published = False
                for rel in self.store.all(
                    "SELECT snapshot FROM releases WHERE revision_id=? AND status=?",
                    (rev["revision_id"], ReleaseStatus.LIVE.value),
                ):
                    if any(i["asset_id"] == asset_id for i in json.loads(rel["snapshot"])["items"]):
                        published = True
                        break
                if published:
                    continue
                self.store.execute(
                    "UPDATE revisions SET status=? WHERE revision_id=?",
                    (RevisionStatus.BLOCKED.value, rev["revision_id"]),
                )
                blocked.append(rev["revision_id"])
            # 在线版本生成范围明确的替换任务；已结束活动保留当时证据。
            tasks = []
            for rel in self.store.all("SELECT * FROM releases WHERE status=?", (ReleaseStatus.LIVE.value,)):
                snapshot = json.loads(rel["snapshot"])
                used = [i for i in snapshot["items"] if i["asset_id"] == asset_id]
                if not used:
                    continue
                if rel["campaign_id"]:
                    campaign = self.store.one(
                        "SELECT status FROM campaigns WHERE campaign_id=?", (rel["campaign_id"],)
                    )
                    if campaign and campaign["status"] == "closed":
                        continue
                task_id = f"task-{event_id}-{rel['release_id']}"
                scope = {
                    "release_id": rel["release_id"],
                    "channel": rel["channel"],
                    "asset_id": asset_id,
                    "segments": [u.get("segment") for u in used],
                    "reason": reason,
                    "deadline_at": deadline_at,
                }
                self.store.execute(
                    "INSERT INTO tasks(task_id, kind, status, scope, created_at) VALUES(?,?,?,?,?)",
                    (task_id, "replacement", TaskStatus.OPEN.value, _canonical(scope), occurred_at),
                )
                self._emit(
                    event_id=f"{event_id}-task-{rel['release_id']}",
                    event_type="REPLACEMENT_TASK_OPENED",
                    aggregate_type="replacement_task",
                    aggregate_id=task_id,
                    occurred_at=occurred_at,
                    payload={"release_ref": rel["release_id"], "asset_ref": asset_id, "scope": scope},
                )
                self.store.execute(
                    "INSERT INTO jobs(job_id, kind, payload, due_at, status) VALUES(?,?,?,?,?)",
                    (
                        f"job-correct-{task_id}",
                        "CORRECTION_PROPAGATION",
                        _canonical(
                            {
                                "release_id": rel["release_id"],
                                "task_id": task_id,
                                "notice": f"素材 {asset_id} 已撤回（{reason}），请在 {deadline_at} 前完成替换",
                            }
                        ),
                        occurred_at,
                        JobStatus.PENDING.value,
                    ),
                )
                tasks.append(task_id)
            self.store.execute(
                "INSERT INTO jobs(job_id, kind, payload, due_at, status) VALUES(?,?,?,?,?)",
                (
                    f"job-deadline-{takedown_id}",
                    "TAKEDOWN_DEADLINE",
                    _canonical({"takedown_id": takedown_id}),
                    deadline_at,
                    JobStatus.PENDING.value,
                ),
            )
            return {
                "asset_id": asset_id,
                "status": AssetStatus.WITHDRAWN.value,
                "takedown_id": takedown_id,
                "blocked_revisions": blocked,
                "replacement_tasks": tasks,
            }

    def complete_replacement(self, *, task_id: str, completed_by: str, occurred_at: str) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            task = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if task is None:
                raise NotFound(f"任务 {task_id} 不存在")
            if task["kind"] != "replacement":
                raise StateConflict(f"任务 {task_id} 不是替换任务")
            if task["status"] != TaskStatus.OPEN.value:
                return {"task_id": task_id, "status": task["status"]}
            self.store.execute("UPDATE tasks SET status=? WHERE task_id=?", (TaskStatus.DONE.value, task_id))
            scope = json.loads(task["scope"])
            remaining = [
                row
                for row in self.store.all("SELECT * FROM tasks WHERE kind='replacement' AND status=?", (TaskStatus.OPEN.value,))
                if json.loads(row["scope"]).get("release_id") == scope["release_id"]
            ]
            release_status = None
            if not remaining:
                cursor = self.store.execute(
                    "UPDATE releases SET status=? WHERE release_id=? AND status=?",
                    (ReleaseStatus.REPLACED.value, scope["release_id"], ReleaseStatus.LIVE.value),
                )
                if cursor.rowcount:
                    release_status = ReleaseStatus.REPLACED.value
            return {"task_id": task_id, "status": TaskStatus.DONE.value, "release_status": release_status}

    def close_campaign(self, *, event_id: str, campaign_id: str, occurred_at: str) -> dict[str, Any]:
        _parse_moment(occurred_at, "occurred_at")
        with self.store.atomic():
            if self._seen(event_id):
                return self._replay(event_id)
            campaign = self.store.one("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,))
            if campaign is None:
                raise NotFound(f"活动 {campaign_id} 不存在")
            if campaign["status"] == "closed":
                return {"campaign_id": campaign_id, "status": "closed", "already_closed": True}
            releases = []
            for rel in self.store.all("SELECT * FROM releases WHERE campaign_id=?", (campaign_id,)):
                releases.append(
                    {
                        "release_id": rel["release_id"],
                        "channel": rel["channel"],
                        "status": rel["status"],
                        "posted_at": rel["posted_at"],
                        "snapshot": json.loads(rel["snapshot"]),
                    }
                )
            evidence = {"campaign_id": campaign_id, "closed_at": occurred_at, "releases": releases}
            self.store.execute(
                "UPDATE campaigns SET status='closed', closed_at=?, evidence=? WHERE campaign_id=?",
                (occurred_at, _canonical(evidence), campaign_id),
            )
            self._emit(
                event_id=event_id,
                event_type="CAMPAIGN_CLOSED",
                aggregate_type="campaign",
                aggregate_id=campaign_id,
                occurred_at=occurred_at,
                payload={"evidence_ref": campaign_id},
            )
            return {"campaign_id": campaign_id, "status": "closed", "evidence": evidence}

    def campaign_evidence(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.store.one("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,))
        if campaign is None:
            raise NotFound(f"活动 {campaign_id} 不存在")
        if campaign["status"] != "closed":
            raise StateConflict(f"活动 {campaign_id} 尚未结束")
        return json.loads(campaign["evidence"])

    # ------------------------------------------------------------------
    # 后台任务：下架期限与更正传播
    # ------------------------------------------------------------------
    def _handle_job(self, job) -> dict[str, Any]:
        payload = json.loads(job["payload"])
        if job["kind"] == "CORRECTION_PROPAGATION":
            self.store.execute(
                "INSERT INTO propagations(release_id, notice, propagated_at) VALUES(?,?,?)",
                (payload["release_id"], payload["notice"], job["due_at"]),
            )
            self._emit(
                event_id=f"{job['job_id']}-event",
                event_type="CORRECTION_PROPAGATED",
                aggregate_type="channel_release",
                aggregate_id=payload["release_id"],
                occurred_at=job["due_at"],
                payload={"release_ref": payload["release_id"], "notice": payload["notice"]},
            )
            return {"propagated": payload["release_id"]}
        if job["kind"] == "TAKEDOWN_DEADLINE":
            takedown = self.store.one("SELECT * FROM takedowns WHERE takedown_id=?", (payload["takedown_id"],))
            if takedown is None:
                raise NotFound(f"下架通知 {payload['takedown_id']} 不存在")
            if takedown["status"] != "open":
                return {"takedown_id": takedown["takedown_id"], "result": "already_resolved"}
            open_tasks = [
                row
                for row in self.store.all(
                    "SELECT * FROM tasks WHERE kind='replacement' AND status=?", (TaskStatus.OPEN.value,)
                )
                if json.loads(row["scope"]).get("asset_id") == takedown["asset_id"]
            ]
            if not open_tasks:
                self.store.execute(
                    "UPDATE takedowns SET status='resolved' WHERE takedown_id=?", (takedown["takedown_id"],)
                )
                return {"takedown_id": takedown["takedown_id"], "result": "resolved"}
            enforced = []
            for row in open_tasks:
                scope = json.loads(row["scope"])
                self.store.execute(
                    "UPDATE releases SET status=? WHERE release_id=? AND status=?",
                    (ReleaseStatus.TAKEN_DOWN.value, scope["release_id"], ReleaseStatus.LIVE.value),
                )
                enforced.append(scope["release_id"])
            self.store.execute(
                "UPDATE takedowns SET status='enforced' WHERE takedown_id=?", (takedown["takedown_id"],)
            )
            self._emit(
                event_id=f"{job['job_id']}-event",
                event_type="TAKEDOWN_ENFORCED",
                aggregate_type="takedown_notice",
                aggregate_id=takedown["takedown_id"],
                occurred_at=job["due_at"],
                payload={"takedown_ref": takedown["takedown_id"], "releases": enforced},
            )
            return {"takedown_id": takedown["takedown_id"], "result": "enforced", "releases": enforced}
        raise StateConflict(f"未知任务类型 {job['kind']}")

    def run_due_jobs(self, now: str | datetime) -> list[dict[str, Any]]:
        now_dt = _parse_moment(now, "now")
        results = []
        for job in self.store.all("SELECT * FROM jobs WHERE status=? ORDER BY due_at", (JobStatus.PENDING.value,)):
            if _parse_moment(job["due_at"], "due_at") > now_dt:
                continue
            with self.store.atomic():
                self.store.execute("UPDATE jobs SET attempts=attempts+1 WHERE job_id=?", (job["job_id"],))
                try:
                    outcome = self._handle_job(job)
                except LineageError as exc:
                    self.store.execute(
                        "UPDATE jobs SET last_error=? WHERE job_id=?", (str(exc), job["job_id"])
                    )
                    results.append({"job_id": job["job_id"], "kind": job["kind"], "status": "retry", "error": str(exc)})
                else:
                    self.store.execute(
                        "UPDATE jobs SET status=?, last_error=NULL WHERE job_id=?",
                        (JobStatus.DONE.value, job["job_id"]),
                    )
                    results.append({"job_id": job["job_id"], "kind": job["kind"], "status": "done", "outcome": outcome})
        return results

    # ------------------------------------------------------------------
    # 成片谱系解释
    # ------------------------------------------------------------------
    def explain_cut(self, ref: str, channel: str | None = None) -> dict[str, Any]:
        release = self.store.one("SELECT * FROM releases WHERE release_id=?", (ref,))
        context: dict[str, Any]
        if release is not None:
            snapshot = json.loads(release["snapshot"])
            items = snapshot["items"]
            channel = release["channel"]
            context = {
                "type": "channel_release",
                "frozen": True,
                "release_id": ref,
                "channel": release["channel"],
                "release_status": release["status"],
                "posted_at": release["posted_at"],
                "propagations": [
                    {"notice": row["notice"], "propagated_at": row["propagated_at"]}
                    for row in self.store.all(
                        "SELECT * FROM propagations WHERE release_id=? ORDER BY id", (ref,)
                    )
                ],
            }
        else:
            revision = self.store.one("SELECT * FROM revisions WHERE revision_id=?", (ref,))
            if revision is None:
                raise NotFound(f"找不到剪辑版本或发布 {ref}")
            items = json.loads(revision["items"])
            context = {
                "type": "edit_revision",
                "frozen": False,
                "revision_id": ref,
                "revision_status": revision["status"],
                "fact_signed_by": revision["fact_signed_by"],
                "commercial_signed_by": revision["commercial_signed_by"],
            }
        explained = []
        issues = []
        for item in items:
            asset = self._asset_or_raise(item["asset_id"])
            contributions = [
                {"creator_id": row["creator_id"], "role": row["role"], "note": row["note"]}
                for row in self.store.all("SELECT * FROM contributions WHERE asset_id=?", (asset["asset_id"],))
            ]
            grants = [
                {
                    "grant_id": row["grant_id"],
                    "grant_kind": row["grant_kind"],
                    "grantor_ref": row["grantor_ref"],
                    "status": row["status"],
                    "scope": json.loads(row["scope"]),
                    "covers_channel": self._grant_covers(json.loads(row["scope"]), channel),
                }
                for row in self.store.all("SELECT * FROM grants WHERE asset_id=?", (asset["asset_id"],))
            ]
            license_status = self._license_status(asset, channel)
            if license_status["status"] != "clear":
                issues.append({"asset_id": asset["asset_id"], "license_status": license_status["status"]})
            params = json.loads(asset["generation_params"]) if asset["generation_params"] else None
            explained.append(
                {
                    "asset_id": asset["asset_id"],
                    "usage": item.get("usage"),
                    "segment": item.get("segment"),
                    "kind": asset["kind"],
                    "source_kind": asset["source_kind"],
                    "asset_status": asset["status"],
                    "content_hash": asset["content_hash"],
                    "generation_params_summary": params,
                    "generation_params_note": GENERATION_PARAMS_NOTE if params is not None else None,
                    "contributions": contributions,
                    "grants": grants,
                    "license": license_status,
                }
            )
        return {
            "ref": ref,
            **context,
            "items": explained,
            "overall_license_status": "clear" if not issues else "attention_required",
            "issues": issues,
            "notes": [GENERATION_PARAMS_NOTE],
        }
