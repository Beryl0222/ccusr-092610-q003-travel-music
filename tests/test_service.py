import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_music.contracts import validate_event
from travel_music.models import LockConflict, PermissionDenied, StateConflict
from travel_music.service import (
    GENERATION_PARAMS_NOTE,
    ROLE_COMMERCIAL_APPROVER,
    ROLE_FACT_CHECKER,
    ROLE_RIGHTS_OFFICER,
    LineageService,
)
from travel_music.store import Store

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
T = "2026-09-26T10:00:00+08:00"


def ts(minute: int) -> str:
    return f"2026-09-26T10:{minute:02d}:00+08:00"


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.svc = LineageService(self.store, SCHEMA)

    def tearDown(self) -> None:
        self.store.close()

    # -- 基础夹具 ------------------------------------------------------
    def _register_music(self, asset_id="music-1", content_hash="hash-music", **overrides):
        params = {
            "event_id": f"ev-reg-{asset_id}",
            "asset_id": asset_id,
            "kind": "music",
            "content_hash": content_hash,
            "source_kind": "ai_generated",
            "declared_by": "ai-ops",
            "occurred_at": ts(1),
            "generation_params": {"model": "melody-x", "seed": 42},
            "license_scope": {"channels": ["douyin"]},
        }
        params.update(overrides)
        return self.svc.register_asset(**params)

    def _register_footage(self, asset_id="foot-1", **overrides):
        params = {
            "event_id": f"ev-reg-{asset_id}",
            "asset_id": asset_id,
            "kind": "footage",
            "content_hash": f"hash-{asset_id}",
            "source_kind": "self_shot",
            "declared_by": "shooter",
            "occurred_at": ts(2),
            "persons_depicted": True,
        }
        params.update(overrides)
        return self.svc.register_asset(**params)

    def _confirm_grant(self, asset_id, grant_kind, grantor, channels=("douyin",), idx="1"):
        self.svc.declare_grant(
            event_id=f"ev-gd-{asset_id}-{grant_kind}",
            grant_id=f"grant-{asset_id}-{grant_kind}",
            asset_id=asset_id,
            grant_kind=grant_kind,
            grantor_ref=grantor,
            scope={"channels": list(channels), "commercial": True},
            declared_by=grantor,
            occurred_at=ts(3),
        )
        return self.svc.confirm_grant(
            event_id=f"ev-gc-{asset_id}-{grant_kind}",
            grant_id=f"grant-{asset_id}-{grant_kind}",
            confirmed_by=grantor,
            confirmer_roles=[],
            occurred_at=ts(4),
        )

    def _approved_revision(self, revision_id="rev-1", items=None):
        if items is None:
            items = [
                {"asset_id": "music-1", "usage": "audio", "segment": "00:00-00:30"},
                {"asset_id": "foot-1", "usage": "footage", "segment": "00:05-00:20"},
            ]
        self.svc.create_revision(
            event_id=f"ev-rev-{revision_id}",
            revision_id=revision_id,
            items=items,
            created_by="editor",
            occurred_at=ts(5),
        )
        self.svc.sign_revision(
            event_id=f"ev-sign-f-{revision_id}",
            revision_id=revision_id,
            signer="checker-1",
            signer_roles=[ROLE_FACT_CHECKER],
            sign_kind="fact",
            occurred_at=ts(6),
        )
        return self.svc.sign_revision(
            event_id=f"ev-sign-c-{revision_id}",
            revision_id=revision_id,
            signer="approver-1",
            signer_roles=[ROLE_COMMERCIAL_APPROVER],
            sign_kind="commercial",
            occurred_at=ts(7),
        )

    def _released(self, release_id="rel-1", revision_id="rev-1", channel="douyin", campaign_id=None):
        return self.svc.post_release(
            event_id=f"ev-rel-{release_id}",
            release_id=release_id,
            revision_id=revision_id,
            channel=channel,
            posted_by="publisher",
            occurred_at=ts(8),
            campaign_id=campaign_id,
        )

    # -- 指纹幂等与隔离审查 ---------------------------------------------
    def test_same_fingerprint_reuses_original_decision(self):
        first = self._register_music()
        self.assertEqual("registered", first["decision"])
        again = self.svc.register_asset(
            event_id="ev-reg-dup",
            asset_id="music-dup",
            kind="music",
            content_hash="hash-music",
            source_kind="ai_generated",
            declared_by="someone-else",
            occurred_at=ts(9),
            license_scope={"channels": ["douyin"]},
        )
        self.assertEqual("reused", again["decision"])
        self.assertEqual("music-1", again["asset_id"])
        count = self.store.one("SELECT COUNT(*) AS c FROM assets")["c"]
        self.assertEqual(1, count)

    def test_same_fingerprint_changed_source_or_scope_is_quarantined(self):
        self._register_music()
        changed_source = self.svc.register_asset(
            event_id="ev-reg-q1",
            asset_id="music-q1",
            kind="music",
            content_hash="hash-music",
            source_kind="public_sample",
            declared_by="sampler",
            occurred_at=ts(9),
            license_scope={"channels": ["douyin"]},
        )
        self.assertEqual("quarantined", changed_source["decision"])
        changed_scope = self.svc.register_asset(
            event_id="ev-reg-q2",
            asset_id="music-q2",
            kind="music",
            content_hash="hash-music",
            source_kind="ai_generated",
            declared_by="ai-ops",
            occurred_at=ts(10),
            license_scope={"channels": ["bilibili"]},
        )
        self.assertEqual("quarantined", changed_scope["decision"])
        reviews = self.store.all("SELECT * FROM tasks WHERE kind='quarantine_review'")
        self.assertEqual(2, len(reviews))
        for asset_id in ("music-q1", "music-q2"):
            row = self.store.one("SELECT status FROM assets WHERE asset_id=?", (asset_id,))
            self.assertEqual("quarantined", row["status"])

    def test_event_id_replay_is_idempotent(self):
        self._register_music()
        replayed = self._register_music()  # 相同 event_id 再次提交
        self.assertEqual("replayed", replayed["decision"])
        self.assertEqual(1, self.store.one("SELECT COUNT(*) AS c FROM assets")["c"])

    # -- 贡献与权利 ------------------------------------------------------
    def test_contribution_must_be_self_declared(self):
        self._register_music()
        with self.assertRaises(PermissionDenied):
            self.svc.declare_contribution(
                event_id="ev-ct-1",
                contribution_id="ct-1",
                creator_id="lyricist",
                asset_id="music-1",
                role="lyrics",
                declared_by="editor",
                occurred_at=ts(9),
            )
        ok = self.svc.declare_contribution(
            event_id="ev-ct-2",
            contribution_id="ct-2",
            creator_id="lyricist",
            asset_id="music-1",
            role="lyrics",
            declared_by="lyricist",
            occurred_at=ts(9),
        )
        self.assertEqual("declared", ok["status"])

    def test_author_cannot_confirm_rights_for_others(self):
        self._register_music()
        self.svc.declare_grant(
            event_id="ev-gd-1",
            grant_id="grant-1",
            asset_id="music-1",
            grant_kind="composition",
            grantor_ref="composer",
            scope={"channels": ["douyin"]},
            declared_by="composer",
            occurred_at=ts(9),
        )
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_grant(
                event_id="ev-gc-1",
                grant_id="grant-1",
                confirmed_by="lyricist",
                confirmer_roles=[],
                occurred_at=ts(10),
            )
        ok = self.svc.confirm_grant(
            event_id="ev-gc-2",
            grant_id="grant-1",
            confirmed_by="officer-1",
            confirmer_roles=[ROLE_RIGHTS_OFFICER],
            occurred_at=ts(10),
        )
        self.assertEqual("confirmed", ok["status"])

    # -- 签署职责分离 ----------------------------------------------------
    def test_fact_and_commercial_signoff_need_distinct_roles_and_people(self):
        self._register_music()
        self.svc.create_revision(
            event_id="ev-rev-1",
            revision_id="rev-1",
            items=[{"asset_id": "music-1", "usage": "audio"}],
            created_by="editor",
            occurred_at=ts(5),
        )
        with self.assertRaises(PermissionDenied):
            self.svc.sign_revision(
                event_id="ev-s-1",
                revision_id="rev-1",
                signer="approver-1",
                signer_roles=[ROLE_COMMERCIAL_APPROVER],
                sign_kind="fact",
                occurred_at=ts(6),
            )
        self.svc.sign_revision(
            event_id="ev-s-2",
            revision_id="rev-1",
            signer="checker-1",
            signer_roles=[ROLE_FACT_CHECKER],
            sign_kind="fact",
            occurred_at=ts(6),
        )
        with self.assertRaises(PermissionDenied):
            self.svc.sign_revision(
                event_id="ev-s-3",
                revision_id="rev-1",
                signer="checker-1",
                signer_roles=[ROLE_FACT_CHECKER, ROLE_COMMERCIAL_APPROVER],
                sign_kind="commercial",
                occurred_at=ts(7),
            )
        done = self.svc.sign_revision(
            event_id="ev-s-4",
            revision_id="rev-1",
            signer="approver-1",
            signer_roles=[ROLE_COMMERCIAL_APPROVER],
            sign_kind="commercial",
            occurred_at=ts(7),
        )
        self.assertEqual("approved", done["status"])

    # -- 发布门槛与冻结 --------------------------------------------------
    def test_generation_params_are_not_rights_proof(self):
        self._register_music()  # AI 生成，带生成参数，但没有词曲授权
        self._approved_revision(items=[{"asset_id": "music-1", "usage": "audio"}])
        with self.assertRaises(StateConflict):
            self._released()
        self._confirm_grant("music-1", "composition", grantor="composer")
        released = self._released()
        self.assertEqual("live", released["status"])

    def test_release_requires_channel_coverage_and_portrait_grant(self):
        self._register_music()
        self._register_footage()
        self._confirm_grant("music-1", "composition", grantor="composer", channels=("douyin",))
        self._confirm_grant("foot-1", "portrait", grantor="resident", channels=("douyin",))
        self._approved_revision()
        with self.assertRaises(StateConflict):  # 授权范围不含 bilibili
            self._released(channel="bilibili")
        released = self._released(channel="douyin")
        self.assertEqual("live", released["status"])

    def test_release_freezes_snapshot(self):
        self._register_music()
        self._register_footage()
        self._confirm_grant("music-1", "composition", grantor="composer")
        self._confirm_grant("foot-1", "portrait", grantor="resident")
        self._approved_revision()
        self._released()
        row = self.store.one("SELECT snapshot FROM releases WHERE release_id='rel-1'")
        snapshot = json.loads(row["snapshot"])
        self.assertEqual(["music-1", "foot-1"], [i["asset_id"] for i in snapshot["items"]])
        self.assertEqual("checker-1", snapshot["fact_signed_by"])
        self.assertEqual("approver-1", snapshot["commercial_signed_by"])
        # 后续撤回不改写已冻结快照
        self.svc.withdraw_source(
            event_id="ev-wd-1",
            asset_id="music-1",
            reason="来源撤回",
            deadline_at="2026-09-30T00:00:00+08:00",
            occurred_at=ts(20),
        )
        again = json.loads(self.store.one("SELECT snapshot FROM releases WHERE release_id='rel-1'")["snapshot"])
        self.assertEqual(snapshot, again)

    # -- 曲目原子锁 ------------------------------------------------------
    def test_track_lock_conflict_and_release(self):
        self._register_music()
        self.svc.create_revision(
            event_id="ev-rev-a", revision_id="rev-a",
            items=[{"asset_id": "music-1", "usage": "audio"}], created_by="e1", occurred_at=ts(5),
        )
        self.svc.create_revision(
            event_id="ev-rev-b", revision_id="rev-b",
            items=[{"asset_id": "music-1", "usage": "audio"}], created_by="e2", occurred_at=ts(5),
        )
        locked = self.svc.acquire_track_lock(
            event_id="ev-lock-a", revision_id="rev-a", track_asset_id="music-1", occurred_at=ts(6)
        )
        self.assertEqual("rev-a", locked["locked_by"])
        with self.assertRaises(LockConflict):
            self.svc.acquire_track_lock(
                event_id="ev-lock-b", revision_id="rev-b", track_asset_id="music-1", occurred_at=ts(7)
            )
        self.svc.release_track_lock(revision_id="rev-a", track_asset_id="music-1")
        again = self.svc.acquire_track_lock(
            event_id="ev-lock-b2", revision_id="rev-b", track_asset_id="music-1", occurred_at=ts(8)
        )
        self.assertEqual("rev-b", again["locked_by"])

    def test_track_lock_is_atomic_across_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lineage.db"
            setup_svc = LineageService(Store(path), SCHEMA)
            setup_svc.register_asset(
                event_id="ev-reg-m", asset_id="music-1", kind="music", content_hash="h",
                source_kind="commissioned", declared_by="c", occurred_at=ts(1),
            )
            for rev in ("rev-a", "rev-b"):
                setup_svc.create_revision(
                    event_id=f"ev-{rev}", revision_id=rev,
                    items=[{"asset_id": "music-1", "usage": "audio"}], created_by="e", occurred_at=ts(2),
                )
            setup_svc.store.close()

            results, errors = [], []

            def worker(name, revision):
                svc = LineageService(Store(path), SCHEMA)
                try:
                    svc.acquire_track_lock(
                        event_id=f"ev-lock-{revision}", revision_id=revision,
                        track_asset_id="music-1", occurred_at=ts(3),
                    )
                    results.append(revision)
                except LockConflict:
                    errors.append(revision)
                finally:
                    svc.store.close()

            threads = [threading.Thread(target=worker, args=(f"w{i}", f"rev-{r}")) for i, r in enumerate(("a", "b"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(1, len(results))
            self.assertEqual(1, len(errors))

    # -- 撤回、替换任务与活动证据 ----------------------------------------
    def _live_release_setup(self, campaign_id=None):
        self._register_music()
        self._register_footage()
        self._confirm_grant("music-1", "composition", grantor="composer")
        self._confirm_grant("foot-1", "portrait", grantor="resident")
        self._approved_revision()
        self._released(campaign_id=campaign_id)

    def test_withdraw_blocks_unpublished_and_tasks_live_release(self):
        self._live_release_setup()
        self.svc.create_revision(
            event_id="ev-rev-2", revision_id="rev-2",
            items=[{"asset_id": "music-1", "usage": "audio"}], created_by="editor", occurred_at=ts(9),
        )
        outcome = self.svc.withdraw_source(
            event_id="ev-wd-1",
            asset_id="music-1",
            reason="委托作曲方撤回授权",
            deadline_at="2026-09-30T00:00:00+08:00",
            occurred_at=ts(10),
        )
        self.assertEqual(["rev-2"], outcome["blocked_revisions"])
        self.assertEqual(["task-ev-wd-1-rel-1"], outcome["replacement_tasks"])
        task = self.store.one("SELECT * FROM tasks WHERE task_id='task-ev-wd-1-rel-1'")
        scope = json.loads(task["scope"])
        self.assertEqual("rel-1", scope["release_id"])
        self.assertEqual("douyin", scope["channel"])
        self.assertEqual(["00:00-00:30"], scope["segments"])
        # 已发布的 rev-1 不被阻止
        rev1 = self.store.one("SELECT status FROM revisions WHERE revision_id='rev-1'")
        self.assertEqual("approved", rev1["status"])
        # 撤回素材不能进入新剪辑
        with self.assertRaises(StateConflict):
            self.svc.create_revision(
                event_id="ev-rev-3", revision_id="rev-3",
                items=[{"asset_id": "music-1", "usage": "audio"}], created_by="editor", occurred_at=ts(11),
            )

    def test_closed_campaign_keeps_evidence_untouched(self):
        self._live_release_setup(campaign_id="camp-mid-autumn")
        closed = self.svc.close_campaign(event_id="ev-camp-1", campaign_id="camp-mid-autumn", occurred_at=ts(12))
        self.assertEqual("closed", closed["status"])
        outcome = self.svc.withdraw_source(
            event_id="ev-wd-1",
            asset_id="music-1",
            reason="来源撤回",
            deadline_at="2026-09-30T00:00:00+08:00",
            occurred_at=ts(13),
        )
        self.assertEqual([], outcome["replacement_tasks"])  # 已结束活动不生成替换任务
        evidence = self.svc.campaign_evidence("camp-mid-autumn")
        self.assertEqual("rel-1", evidence["releases"][0]["release_id"])
        self.assertEqual(
            ["music-1", "foot-1"],
            [i["asset_id"] for i in evidence["releases"][0]["snapshot"]["items"]],
        )

    # -- 后台任务中断恢复 --------------------------------------------------
    def test_jobs_resume_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lineage.db"
            svc = LineageService(Store(path), SCHEMA)
            svc.register_asset(
                event_id="ev-reg-m", asset_id="music-1", kind="music", content_hash="h",
                source_kind="commissioned", declared_by="c", occurred_at=ts(1),
            )
            svc.declare_grant(
                event_id="ev-gd", grant_id="grant-1", asset_id="music-1", grant_kind="composition",
                grantor_ref="composer", scope={"channels": ["douyin"]}, declared_by="composer", occurred_at=ts(2),
            )
            svc.confirm_grant(
                event_id="ev-gc", grant_id="grant-1", confirmed_by="composer",
                confirmer_roles=[], occurred_at=ts(3),
            )
            svc.create_revision(
                event_id="ev-rev", revision_id="rev-1",
                items=[{"asset_id": "music-1", "usage": "audio", "segment": "00:00-00:30"}],
                created_by="editor", occurred_at=ts(4),
            )
            svc.sign_revision(
                event_id="ev-sf", revision_id="rev-1", signer="checker-1",
                signer_roles=[ROLE_FACT_CHECKER], sign_kind="fact", occurred_at=ts(5),
            )
            svc.sign_revision(
                event_id="ev-sc", revision_id="rev-1", signer="approver-1",
                signer_roles=[ROLE_COMMERCIAL_APPROVER], sign_kind="commercial", occurred_at=ts(6),
            )
            svc.post_release(
                event_id="ev-rel", release_id="rel-1", revision_id="rev-1",
                channel="douyin", posted_by="publisher", occurred_at=ts(7),
            )
            svc.withdraw_source(
                event_id="ev-wd", asset_id="music-1", reason="授权到期",
                deadline_at="2026-09-30T00:00:00+08:00", occurred_at=ts(8),
            )
            svc.store.close()  # 模拟中断：不处理任何后台任务直接退出

            resumed = LineageService(Store(path), SCHEMA)
            results = resumed.run_due_jobs("2026-10-01T00:00:00+08:00")
            kinds = {(r["kind"], r["status"]) for r in results}
            self.assertIn(("CORRECTION_PROPAGATION", "done"), kinds)
            self.assertIn(("TAKEDOWN_DEADLINE", "done"), kinds)
            enforced = [r for r in results if r["kind"] == "TAKEDOWN_DEADLINE"][0]
            self.assertEqual("enforced", enforced["outcome"]["result"])
            release = resumed.store.one("SELECT status FROM releases WHERE release_id='rel-1'")
            self.assertEqual("taken_down", release["status"])
            explained = resumed.explain_cut("rel-1")
            self.assertEqual(1, len(explained["propagations"]))
            self.assertIn("授权到期", explained["propagations"][0]["notice"])
            # 任务完成后再次运行为空
            self.assertEqual([], resumed.run_due_jobs("2026-10-01T01:00:00+08:00"))
            resumed.store.close()

    def test_replacement_completion_marks_release_replaced(self):
        self._live_release_setup()
        self.svc.withdraw_source(
            event_id="ev-wd-1", asset_id="music-1", reason="撤回",
            deadline_at="2026-09-30T00:00:00+08:00", occurred_at=ts(10),
        )
        done = self.svc.complete_replacement(
            task_id="task-ev-wd-1-rel-1", completed_by="editor", occurred_at=ts(11)
        )
        self.assertEqual("replaced", done["release_status"])
        results = self.svc.run_due_jobs("2026-10-01T00:00:00+08:00")
        deadline = [r for r in results if r["kind"] == "TAKEDOWN_DEADLINE"][0]
        self.assertEqual("resolved", deadline["outcome"]["result"])
        release = self.store.one("SELECT status FROM releases WHERE release_id='rel-1'")
        self.assertEqual("replaced", release["status"])

    # -- 成片解释 --------------------------------------------------------
    def test_explain_cut_reports_sources_and_license(self):
        self._live_release_setup()
        self.svc.declare_contribution(
            event_id="ev-ct-1", contribution_id="ct-1", creator_id="ai-ops",
            asset_id="music-1", role="ai_ops", declared_by="ai-ops", occurred_at=ts(9),
        )
        explained = self.svc.explain_cut("rel-1")
        self.assertTrue(explained["frozen"])
        self.assertEqual("clear", explained["overall_license_status"])
        music = next(i for i in explained["items"] if i["asset_id"] == "music-1")
        self.assertEqual("ai_generated", music["source_kind"])
        self.assertEqual({"model": "melody-x", "seed": 42}, music["generation_params_summary"])
        self.assertEqual(GENERATION_PARAMS_NOTE, music["generation_params_note"])
        self.assertEqual([{"creator_id": "ai-ops", "role": "ai_ops", "note": None}], music["contributions"])
        self.assertIn(GENERATION_PARAMS_NOTE, explained["notes"])

    def test_explain_draft_revision_flags_missing_grant(self):
        self._register_music()
        self.svc.create_revision(
            event_id="ev-rev-1", revision_id="rev-1",
            items=[{"asset_id": "music-1", "usage": "audio"}], created_by="editor", occurred_at=ts(5),
        )
        explained = self.svc.explain_cut("rev-1", channel="douyin")
        self.assertFalse(explained["frozen"])
        self.assertEqual("attention_required", explained["overall_license_status"])
        self.assertEqual("missing_grant", explained["items"][0]["license"]["status"])

    # -- 时间约束 --------------------------------------------------------
    def test_occurred_at_requires_timezone(self):
        with self.assertRaises(ValueError):
            self.svc.register_asset(
                event_id="ev-tz", asset_id="a-1", kind="copy", content_hash="h",
                source_kind="self_shot", declared_by="w", occurred_at="2026-09-26T10:00:00",
            )

    def test_events_are_contract_valid_and_versioned(self):
        self._live_release_setup()
        events = self.svc.list_events()
        self.assertTrue(events)
        for event in events:
            self.assertEqual([], validate_event(event, SCHEMA))
        revision_events = [e for e in events if e["aggregate_type"] == "edit_revision"]
        self.assertEqual([1, 2, 3, 4], [e["version"] for e in revision_events])


if __name__ == "__main__":
    unittest.main()
