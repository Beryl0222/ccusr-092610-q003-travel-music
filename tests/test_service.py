import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_music.contracts import validate_event
from travel_music.models import (
    Actor,
    ChannelPolicy,
    ConflictError,
    PermissionDenied,
    ServiceError,
    StateError,
)
from travel_music.service import ProvenanceService, load_schema
from travel_music.store import Store
from travel_music.worker import JobWorker

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 26, 10, 0, tzinfo=CST)


def at(**delta):
    return T0 + timedelta(**delta)


AUTHOR = Actor("u-author", {"author"})
FACT = Actor("u-fact", {"fact_checker"})
FACT2 = Actor("u-fact2", {"fact_checker"})
REL = Actor("u-rel", {"release_manager"})
REL2 = Actor("u-rel2", {"release_manager"})
RIGHTS = Actor("u-rights", {"rights_officer"})
BOTH = Actor("u-both", {"fact_checker", "release_manager"})

POLICIES = [
    ChannelPolicy("short-video", allow_ai_music=True, requires_portrait_grant=True, commercial=True),
    ChannelPolicy("tv", allow_ai_music=False, requires_portrait_grant=True, commercial=True),
]

SCOPE_ALL = {"channels": ["all"], "commercial": True}
AI_PARAMS = {"model": "melody-ai", "prompt_digest": "sha256:abc", "seed": 42}


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.service = ProvenanceService(self.store, POLICIES, load_schema())
        self.worker = JobWorker(self.service)

    def tearDown(self):
        self.store.close()

    # ------------------------------------------------------------------
    # 构造工具
    # ------------------------------------------------------------------
    def audio_asset(self, content_hash="h-audio", source="commissioned", scope=None,
                    grantor="composer-1", verify=True, **kwargs):
        result = self.service.register_asset(AUTHOR, "audio", content_hash, source, now=T0, **kwargs)
        if verify:
            grant = self.service.declare_rights(
                Actor(grantor), result.asset_id, "composition", grantor, scope or SCOPE_ALL, now=T0
            )
            self.service.verify_rights(FACT, grant, now=T0)
        return result.asset_id

    def video_asset(self, content_hash="h-video", subjects=("person-a",), verify=True):
        result = self.service.register_asset(
            AUTHOR, "video", content_hash, "self_recorded",
            portrait_subjects=list(subjects), now=T0,
        )
        if verify:
            for subject in subjects:
                grant = self.service.declare_rights(
                    Actor(subject), result.asset_id, "portrait", subject, SCOPE_ALL, now=T0
                )
                self.service.verify_rights(FACT, grant, now=T0)
        return result.asset_id

    def ready_revision(self, items):
        revision = self.service.create_revision(AUTHOR, items, now=T0)
        self.service.signoff(FACT, revision, "fact", now=T0)
        self.service.signoff(REL, revision, "commercial", now=T0)
        return revision

    def film_items(self, video, audio):
        return [
            {"kind": "video", "asset_ref": video},
            {"kind": "audio", "asset_ref": audio},
            {"kind": "description", "text": "片尾限定：含 AI 生成旋律，禁止二次商用"},
        ]


class RightsDeclarationTests(ServiceTestCase):
    def test_author_declares_own_rights_but_not_for_others(self):
        asset = self.audio_asset(verify=False)
        grant = self.service.declare_rights(
            Actor("composer-1"), asset, "composition", "composer-1", SCOPE_ALL, now=T0
        )
        self.assertTrue(grant.startswith("grant_"))
        with self.assertRaises(PermissionDenied):
            self.service.declare_rights(
                AUTHOR, asset, "composition", "composer-1", SCOPE_ALL, now=T0
            )

    def test_declarant_cannot_verify_own_declaration(self):
        asset = self.audio_asset(verify=False)
        grant = self.service.declare_rights(
            Actor("composer-1"), asset, "composition", "composer-1", SCOPE_ALL, now=T0
        )
        declarant_as_checker = Actor("composer-1", {"fact_checker"})
        with self.assertRaises(PermissionDenied):
            self.service.verify_rights(declarant_as_checker, grant, now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.verify_rights(AUTHOR, grant, now=T0)  # 无核验角色
        self.service.verify_rights(FACT, grant, now=T0)
        with self.assertRaises(StateError):
            self.service.verify_rights(FACT2, grant, now=T0)  # 重复核验

    def test_contribution_is_self_declared_only(self):
        asset = self.audio_asset()
        self.service.record_contribution(AUTHOR, asset, "editing", now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.record_contribution(
                AUTHOR, asset, "composition", contributor_ref="someone-else", now=T0
            )


class SignoffTests(ServiceTestCase):
    def test_fact_and_commercial_need_distinct_roles_and_people(self):
        audio = self.audio_asset()
        video = self.video_asset()
        revision = self.service.create_revision(AUTHOR, self.film_items(video, audio), now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.signoff(AUTHOR, revision, "fact", now=T0)  # 无角色
        with self.assertRaises(PermissionDenied):
            self.service.signoff(FACT, revision, "commercial", now=T0)  # 角色不符
        self.service.signoff(BOTH, revision, "fact", now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.signoff(BOTH, revision, "commercial", now=T0)  # 同一人
        self.service.signoff(REL, revision, "commercial", now=T0)
        state = self.store.read_one("SELECT state FROM revisions WHERE revision_id = ?", (revision,))
        self.assertEqual("ready", state["state"])
        frozen = [
            e for e in self.service.list_events("edit_revision", revision)
            if e["event_type"] == "REVISION_FROZEN"
        ]
        self.assertEqual(1, len(frozen))


class ReleaseFreezeTests(ServiceTestCase):
    def test_release_freezes_actual_items_and_notices(self):
        audio = self.audio_asset()
        video = self.video_asset()
        revision = self.ready_revision(self.film_items(video, audio))
        override = [
            {"kind": "video", "asset_ref": video},
            {"kind": "description", "text": "平台二剪版：片尾限定被截掉后的实际文案"},
        ]
        notices = [{"kind": "ai_disclosure", "text": "含 AI 生成旋律"}]
        release = self.service.post_release(
            REL, revision, "short-video", items_override=override, notices=notices, now=T0
        )
        row = self.store.read_one("SELECT * FROM releases WHERE release_id = ?", (release,))
        snapshot = json.loads(row["frozen_snapshot"])
        self.assertEqual(2, len(snapshot["items"]))
        self.assertEqual("平台二剪版：片尾限定被截掉后的实际文案", snapshot["items"][1]["text"])
        self.assertEqual(notices, snapshot["notices"])
        posted = [
            e for e in self.service.list_events("channel_release", release)
            if e["event_type"] == "RELEASE_POSTED"
        ][0]
        self.assertEqual(snapshot["items"], posted["payload"]["frozen_items"])
        with self.assertRaises(StateError):
            self.service.post_release(REL, revision, "tv", now=T0)  # 已发布不可重复

    def test_override_cannot_introduce_foreign_assets(self):
        audio = self.audio_asset()
        video = self.video_asset()
        foreign = self.video_asset(content_hash="h-foreign")
        revision = self.ready_revision(self.film_items(video, audio))
        with self.assertRaises(ServiceError):
            self.service.post_release(
                REL, revision, "short-video",
                items_override=[{"kind": "video", "asset_ref": foreign}], now=T0,
            )


class ChannelPolicyTests(ServiceTestCase):
    def test_channel_policy_blocks_ai_music(self):
        ai_audio = self.audio_asset(
            content_hash="h-ai", source="ai_generated", generation_params=AI_PARAMS
        )
        video = self.video_asset()
        revision = self.ready_revision(self.film_items(video, ai_audio))
        with self.assertRaises(StateError) as ctx:
            self.service.post_release(REL, revision, "tv", now=T0)
        self.assertIn("AI", str(ctx.exception))
        release = self.service.post_release(REL, revision, "short-video", now=T0)
        self.assertTrue(release.startswith("rel_"))

    def test_portrait_grant_required_for_commercial_channel(self):
        audio = self.audio_asset()
        video = self.video_asset(verify=False)  # 画面中人物未授权
        revision = self.ready_revision(self.film_items(video, audio))
        with self.assertRaises(StateError) as ctx:
            self.service.post_release(REL, revision, "short-video", now=T0)
        self.assertIn("肖像授权", str(ctx.exception))

    def test_grant_scope_must_cover_channel(self):
        video = self.video_asset()
        limited = {"channels": ["tv"], "commercial": True}
        audio = self.audio_asset(content_hash="h-limited", scope=limited)
        revision = self.ready_revision(self.film_items(video, audio))
        with self.assertRaises(StateError) as ctx:
            self.service.post_release(REL, revision, "short-video", now=T0)
        self.assertIn("词曲授权", str(ctx.exception))


class GenerationParamsTests(ServiceTestCase):
    def test_generation_params_are_not_rights_proof(self):
        ai_audio = self.audio_asset(
            content_hash="h-ai-nogrant", source="ai_generated",
            generation_params=AI_PARAMS, verify=False,
        )
        video = self.video_asset()
        revision = self.ready_revision(self.film_items(video, ai_audio))
        with self.assertRaises(StateError) as ctx:
            self.service.post_release(REL, revision, "short-video", now=T0)
        self.assertIn("词曲授权", str(ctx.exception))

    def test_generation_params_only_on_ai_assets(self):
        with self.assertRaises(ServiceError):
            self.service.register_asset(
                AUTHOR, "audio", "h-x", "commissioned", generation_params=AI_PARAMS, now=T0
            )


class FingerprintTests(ServiceTestCase):
    def test_duplicate_fingerprint_reuses_original_decision(self):
        first = self.service.register_asset(
            AUTHOR, "audio", "h-dup", "commissioned", declared_scope=SCOPE_ALL, now=T0
        )
        again = self.service.register_asset(
            AUTHOR, "audio", "h-dup", "commissioned", declared_scope=SCOPE_ALL, now=at(minutes=1)
        )
        self.assertTrue(again.reused)
        self.assertEqual(first.asset_id, again.asset_id)
        self.service.withdraw_source(RIGHTS, first.asset_id, "版权方撤回", now=at(minutes=2))
        third = self.service.register_asset(
            AUTHOR, "audio", "h-dup", "commissioned", declared_scope=SCOPE_ALL, now=at(minutes=3)
        )
        self.assertTrue(third.reused)
        self.assertEqual("withdrawn", third.state)  # 沿用原决定
        dedup_events = [
            e for e in self.service.list_events("media_asset", first.asset_id)
            if e["event_type"] == "ASSET_REGISTRATION_DEDUPED"
        ]
        self.assertEqual(2, len(dedup_events))

    def test_fingerprint_drift_goes_to_quarantine(self):
        self.service.register_asset(
            AUTHOR, "audio", "h-drift", "commissioned", declared_scope=SCOPE_ALL, now=T0
        )
        drifted = self.service.register_asset(
            AUTHOR, "audio", "h-drift", "public_sample", declared_scope=SCOPE_ALL, now=at(minutes=1)
        )
        self.assertEqual("quarantined", drifted.state)
        drifted_scope = self.service.register_asset(
            AUTHOR, "audio", "h-drift", "commissioned",
            declared_scope={"channels": ["tv"], "commercial": False}, now=at(minutes=2),
        )
        self.assertEqual("quarantined", drifted_scope.state)
        quarantined = [
            e for e in self.service.list_events("media_asset", drifted.asset_id)
            if e["event_type"] == "ASSET_QUARANTINED"
        ][0]
        self.assertEqual(["source_kind"], quarantined["payload"]["drift_fields"])

    def test_quarantined_asset_blocked_until_resolved(self):
        self.service.register_asset(AUTHOR, "audio", "h-q", "commissioned", now=T0)
        drifted = self.service.register_asset(
            AUTHOR, "audio", "h-q", "ai_generated", now=at(minutes=1)
        )
        grant = self.service.declare_rights(
            Actor("composer-1"), drifted.asset_id, "composition", "composer-1", SCOPE_ALL, now=T0
        )
        self.service.verify_rights(FACT, grant, now=T0)
        video = self.video_asset()
        revision = self.ready_revision(self.film_items(video, drifted.asset_id))
        with self.assertRaises(StateError):
            self.service.post_release(REL, revision, "short-video", now=T0)
        with self.assertRaises(PermissionDenied):
            self.service.resolve_quarantine(REL, drifted.asset_id, "cleared", now=T0)
        self.service.resolve_quarantine(FACT, drifted.asset_id, "cleared", now=at(minutes=2))
        release = self.service.post_release(REL, revision, "short-video", now=at(minutes=3))
        self.assertTrue(release.startswith("rel_"))


class TrackLockTests(ServiceTestCase):
    def test_reservation_is_all_or_nothing(self):
        t1 = self.audio_asset(content_hash="h-t1")
        t2 = self.audio_asset(content_hash="h-t2")
        t3 = self.audio_asset(content_hash="h-t3")
        rev_a = self.service.create_revision(
            AUTHOR, [{"kind": "audio", "asset_ref": t1}, {"kind": "audio", "asset_ref": t2}], now=T0
        )
        rev_b = self.service.create_revision(
            AUTHOR, [{"kind": "audio", "asset_ref": t2}, {"kind": "audio", "asset_ref": t3}], now=T0
        )
        self.service.reserve_tracks(AUTHOR, rev_a, [t1, t2], now=T0)
        with self.assertRaises(ConflictError):
            self.service.reserve_tracks(AUTHOR, rev_b, [t3, t2], now=at(minutes=1))
        locks = self.store.read("SELECT * FROM track_locks")
        self.assertEqual({t1, t2}, {row["track_ref"] for row in locks})  # t3 未被部分锁定
        self.service.abandon_revision(AUTHOR, rev_a, now=at(minutes=2))
        self.service.reserve_tracks(AUTHOR, rev_b, [t2, t3], now=at(minutes=3))

    def test_reservation_requires_track_in_revision(self):
        t1 = self.audio_asset(content_hash="h-r1")
        t2 = self.audio_asset(content_hash="h-r2")
        revision = self.service.create_revision(
            AUTHOR, [{"kind": "audio", "asset_ref": t1}], now=T0
        )
        with self.assertRaises(ServiceError):
            self.service.reserve_tracks(AUTHOR, revision, [t2], now=T0)

    def test_concurrent_reservation_is_atomic(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "locks.db")
            store1, store2 = Store(path), Store(path)
            try:
                svc1 = ProvenanceService(store1, POLICIES, load_schema())
                svc2 = ProvenanceService(store2, POLICIES, load_schema())
                track = svc1.register_asset(AUTHOR, "audio", "h-race", "commissioned", now=T0).asset_id
                rev1 = svc1.create_revision(AUTHOR, [{"kind": "audio", "asset_ref": track}], now=T0)
                rev2 = svc2.create_revision(AUTHOR, [{"kind": "audio", "asset_ref": track}], now=T0)
                barrier = threading.Barrier(2)
                outcomes = {}

                def reserve(service, revision, key):
                    barrier.wait(timeout=10)
                    try:
                        service.reserve_tracks(AUTHOR, revision, [track], now=T0)
                        outcomes[key] = "ok"
                    except ConflictError:
                        outcomes[key] = "conflict"

                threads = [
                    threading.Thread(target=reserve, args=(svc1, rev1, "a")),
                    threading.Thread(target=reserve, args=(svc2, rev2, "b")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertEqual(["conflict", "ok"], sorted(outcomes.values()))
                locks = store1.read("SELECT * FROM track_locks")
                self.assertEqual(1, len(locks))
            finally:
                store1.close()
                store2.close()


class WithdrawalTests(ServiceTestCase):
    def _setup_campaign(self):
        audio = self.audio_asset(content_hash="h-w")
        video = self.video_asset()
        draft = self.service.create_revision(AUTHOR, self.film_items(video, audio), now=T0)
        live_rev = self.ready_revision(self.film_items(video, audio))
        live = self.service.post_release(REL, live_rev, "short-video", now=T0)
        ended_rev = self.ready_revision(self.film_items(video, audio))
        ended = self.service.post_release(REL, ended_rev, "tv", now=T0)
        self.service.end_release(REL, ended, now=at(hours=1))
        return audio, video, draft, live, ended

    def test_withdrawal_blocks_unpublished_and_tasks_live_releases(self):
        audio, video, draft, live, ended = self._setup_campaign()
        result = self.service.withdraw_source(
            RIGHTS, audio, "采样来源撤回授权", replacement_due_at=at(hours=24), now=at(hours=2)
        )
        self.assertEqual([draft], result["blocked_revisions"])
        self.assertEqual(1, len(result["replacement_tasks"]))
        draft_row = self.store.read_one("SELECT * FROM revisions WHERE revision_id = ?", (draft,))
        self.assertEqual("blocked", draft_row["state"])
        task = self.store.read_one(
            "SELECT * FROM replacement_tasks WHERE task_id = ?",
            (result["replacement_tasks"][0],),
        )
        scope = json.loads(task["scope"])
        self.assertEqual(live, scope["release_ref"])
        self.assertEqual("short-video", scope["channel"])
        self.assertEqual(["audio"], [item["kind"] for item in scope["items"]])
        self.assertEqual(audio, scope["items"][0]["asset_ref"])
        ended_row = self.store.read_one("SELECT * FROM releases WHERE release_id = ?", (ended,))
        self.assertEqual("ended", ended_row["state"])  # 已结束活动不动
        explanation = self.service.explain_release(ended, now=at(hours=3))
        self.assertEqual(3, len(explanation["items"]))  # 当时证据保留
        live_row = self.store.read_one("SELECT * FROM releases WHERE release_id = ?", (live,))
        self.assertEqual("live", live_row["state"])  # 未到期不自动下架

    def test_withdrawal_requires_rights_officer(self):
        audio = self.audio_asset()
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_source(AUTHOR, audio, "x", now=T0)


class BackgroundJobTests(ServiceTestCase):
    def _live_release(self, service, suffix=""):
        audio = service.register_asset(AUTHOR, "audio", f"h-bg{suffix}", "commissioned", now=T0)
        grant = service.declare_rights(
            Actor("composer-1"), audio.asset_id, "composition", "composer-1", SCOPE_ALL, now=T0
        )
        service.verify_rights(FACT, grant, now=T0)
        video = service.register_asset(
            AUTHOR, "video", f"h-bgv{suffix}", "self_recorded", now=T0
        )
        revision = service.create_revision(
            AUTHOR,
            [{"kind": "video", "asset_ref": video.asset_id},
             {"kind": "audio", "asset_ref": audio.asset_id}],
            now=T0,
        )
        service.signoff(FACT, revision, "fact", now=T0)
        service.signoff(REL, revision, "commercial", now=T0)
        return service.post_release(REL, revision, "short-video", now=T0), audio.asset_id

    def test_replacement_deadline_enforced_after_restart(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "jobs.db")
            store = Store(path)
            service = ProvenanceService(store, POLICIES, load_schema())
            live, audio = self._live_release(service)
            service.withdraw_source(
                RIGHTS, audio, "来源撤回", replacement_due_at=at(hours=24), now=at(hours=1)
            )
            store.close()  # 模拟进程中断
            store2 = Store(path)
            try:
                service2 = ProvenanceService(store2, POLICIES, load_schema())
                worker2 = JobWorker(service2)  # 新实例继续处理
                processed = worker2.run_pending(at(hours=25))
                self.assertEqual(1, len(processed))
                row = store2.read_one("SELECT state FROM releases WHERE release_id = ?", (live,))
                self.assertEqual("suspended", row["state"])
                suspended = [
                    e for e in service2.list_events("channel_release", live)
                    if e["event_type"] == "RELEASE_SUSPENDED"
                ]
                self.assertEqual(1, len(suspended))
            finally:
                store2.close()

    def test_correction_propagates_after_restart(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "corr.db")
            store = Store(path)
            service = ProvenanceService(store, POLICIES, load_schema())
            live, _ = self._live_release(service)
            service.issue_correction(
                FACT, live, {"field": "credit", "note": "作曲署名更正"}, now=at(hours=1)
            )
            store.close()  # 更正在线但尚未传播时中断
            store2 = Store(path)
            try:
                service2 = ProvenanceService(store2, POLICIES, load_schema())
                JobWorker(service2).run_pending(at(hours=2))
                propagated = [
                    e for e in service2.list_events("channel_release", live)
                    if e["event_type"] == "CORRECTION_PROPAGATED"
                ]
                self.assertEqual(1, len(propagated))
                self.assertEqual("short-video", propagated[0]["payload"]["channel"])
            finally:
                store2.close()

    def test_correction_on_ended_release_does_not_propagate(self):
        live, _ = self._live_release(self.service)
        self.service.end_release(REL, live, now=at(hours=1))
        self.service.issue_correction(
            FACT, live, {"field": "credit", "note": "存档更正"}, now=at(hours=2)
        )
        processed = self.worker.run_pending(at(hours=3))
        self.assertEqual([], processed)

    def test_takedown_deadline_enforced_by_worker(self):
        live, audio = self._live_release(self.service)
        self.service.open_takedown(
            RIGHTS, "release", live, "监管要求下架", at(hours=12), now=at(hours=1)
        )
        self.assertEqual([], self.worker.run_pending(at(hours=2)))  # 未到期
        self.worker.run_pending(at(hours=13))
        row = self.store.read_one("SELECT state FROM releases WHERE release_id = ?", (live,))
        self.assertEqual("suspended", row["state"])
        enforced = [
            e for e in self.service.list_events("takedown_notice")
            if e["event_type"] == "TAKEDOWN_ENFORCED"
        ]
        self.assertEqual(1, len(enforced))
        self.assertEqual([live], enforced[0]["payload"]["affected_releases"])

    def test_takedown_on_asset_skips_ended_releases(self):
        live, audio = self._live_release(self.service, suffix="1")
        ended, _ = self._live_release(self.service, suffix="2")
        # 让 ended 也使用同一音频：重新发布一条使用该音频的成片并结束
        video2 = self.service.register_asset(
            AUTHOR, "video", "h-bgv3", "self_recorded", now=T0
        ).asset_id
        rev = self.service.create_revision(
            AUTHOR,
            [{"kind": "video", "asset_ref": video2}, {"kind": "audio", "asset_ref": audio}],
            now=T0,
        )
        self.service.signoff(FACT, rev, "fact", now=T0)
        self.service.signoff(REL, rev, "commercial", now=T0)
        ended_with_audio = self.service.post_release(REL, rev, "tv", now=T0)
        self.service.end_release(REL, ended_with_audio, now=at(hours=1))
        self.service.open_takedown(
            RIGHTS, "asset", audio, "版权方要求全面下架", at(hours=2), now=at(hours=1)
        )
        self.worker.run_pending(at(hours=3))
        live_row = self.store.read_one("SELECT state FROM releases WHERE release_id = ?", (live,))
        self.assertEqual("suspended", live_row["state"])
        ended_row = self.store.read_one(
            "SELECT state FROM releases WHERE release_id = ?", (ended_with_audio,)
        )
        self.assertEqual("ended", ended_row["state"])  # 已结束活动保留证据
        asset_row = self.store.read_one("SELECT state FROM assets WHERE asset_id = ?", (audio,))
        self.assertEqual("withdrawn", asset_row["state"])

    def test_stale_claimed_job_is_recovered(self):
        live, _ = self._live_release(self.service)
        self.service.issue_correction(
            FACT, live, {"field": "credit", "note": "署名更正"}, now=at(hours=1)
        )
        # 模拟 worker 认领后崩溃：任务停留在 claimed
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'claimed', claimed_by = 'crashed-worker',"
                " claimed_at = ? WHERE status = 'pending'",
                (at(hours=1).isoformat(),),
            )
        recovered = JobWorker(self.service).recover_stale(at(hours=3), claim_timeout_seconds=60)
        self.assertEqual(1, recovered)
        JobWorker(self.service).run_pending(at(hours=3))
        propagated = [
            e for e in self.service.list_events("channel_release", live)
            if e["event_type"] == "CORRECTION_PROPAGATED"
        ]
        self.assertEqual(1, len(propagated))


class ExplainTests(ServiceTestCase):
    def test_explain_release_reports_sources_and_license_status(self):
        ai_audio = self.audio_asset(
            content_hash="h-exp-ai", source="ai_generated", generation_params=AI_PARAMS
        )
        video = self.video_asset(subjects=("person-a", "person-b"))
        self.service.record_contribution(Actor("composer-1"), ai_audio, "composition", now=T0)
        self.service.record_contribution(AUTHOR, video, "filming", now=T0)
        revision = self.ready_revision(self.film_items(video, ai_audio))
        release = self.service.post_release(
            REL, revision, "short-video",
            notices=[{"kind": "ai_disclosure", "text": "含 AI 生成旋律"}], now=T0,
        )
        explanation = self.service.explain_release(release, now=at(hours=1))
        self.assertEqual("clear", explanation["license_status"])
        self.assertEqual(
            [{"kind": "ai_disclosure", "text": "含 AI 生成旋律"}], explanation["notices"]
        )
        by_kind = {item["kind"]: item for item in explanation["items"]}
        ai_item = by_kind["audio"]
        self.assertEqual("ai_generated", ai_item["source_kind"])
        self.assertFalse(ai_item["generation_params_rights_proof"])
        self.assertEqual(AI_PARAMS, ai_item["generation_params"])
        self.assertEqual("verified", ai_item["rights"][0]["state"])
        video_item = by_kind["video"]
        self.assertEqual(
            {"person-a", "person-b"},
            {right["grantor_ref"] for right in video_item["rights"]},
        )
        self.assertEqual(
            [("u-author", "filming")],
            [(c["contributor_ref"], c["role"]) for c in video_item["contributors"]],
        )
        self.service.withdraw_source(RIGHTS, ai_audio, "AI 素材来源存疑", now=at(hours=2))
        after = self.service.explain_release(release, now=at(hours=3))
        self.assertEqual("issues", after["license_status"])
        self.assertEqual(1, len(after["open_replacement_tasks"]))
        self.assertEqual("withdrawn", after["items"][1]["asset_state"])

    def test_explain_asset_marks_generation_params(self):
        ai_audio = self.audio_asset(
            content_hash="h-exp-asset", source="ai_generated", generation_params=AI_PARAMS
        )
        explanation = self.service.explain_asset(ai_audio)
        self.assertFalse(explanation["generation_params_rights_proof"])
        self.assertEqual("ai_generated", explanation["source_kind"])
        self.assertEqual("ASSET_REGISTERED", explanation["events"][0]["event_type"])


class ContractConformanceTests(ServiceTestCase):
    def test_all_emitted_events_conform_to_contract(self):
        schema = load_schema()
        ai_audio = self.audio_asset(
            content_hash="h-all", source="ai_generated", generation_params=AI_PARAMS
        )
        video = self.video_asset()
        self.service.record_contribution(AUTHOR, video, "filming", now=T0)
        revision = self.service.create_revision(AUTHOR, self.film_items(video, ai_audio), now=T0)
        self.service.reserve_tracks(AUTHOR, revision, [ai_audio], now=T0)
        self.service.signoff(FACT, revision, "fact", now=T0)
        self.service.signoff(REL, revision, "commercial", now=T0)
        release = self.service.post_release(
            REL, revision, "short-video",
            notices=[{"kind": "ai_disclosure", "text": "含 AI 生成旋律"}], now=T0,
        )
        self.service.issue_correction(
            FACT, release, {"field": "credit", "note": "署名更正"}, now=at(hours=1)
        )
        self.service.withdraw_source(
            RIGHTS, ai_audio, "来源撤回", replacement_due_at=at(hours=24), now=at(hours=2)
        )
        self.service.open_takedown(
            RIGHTS, "release", release, "监管通知", at(hours=48), now=at(hours=3)
        )
        self.worker.run_pending(at(hours=49))
        events = self.service.list_events()
        self.assertGreater(len(events), 10)
        for event in events:
            self.assertEqual([], validate_event(event, schema), event["event_type"])

    def test_naive_time_is_rejected(self):
        naive = datetime(2026, 9, 26, 10, 0)
        with self.assertRaises(ValueError):
            self.service.register_asset(AUTHOR, "audio", "h-naive", "commissioned", now=naive)


if __name__ == "__main__":
    unittest.main()
