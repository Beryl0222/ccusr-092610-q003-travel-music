"""端到端演示：从素材登记到渠道发布、撤回与下架处理。

运行方式：PYTHONPATH=src python3 examples/walkthrough.py
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travel_music import Actor, ChannelPolicy, JobWorker, ProvenanceService, Store

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 26, 10, 0, tzinfo=CST)


def at(**delta):
    return T0 + timedelta(**delta)


def show(title, payload):
    print(f"\n== {title} ==")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main():
    policies = [
        ChannelPolicy("short-video", allow_ai_music=True, requires_portrait_grant=True),
        ChannelPolicy("tv", allow_ai_music=False, requires_portrait_grant=True),
    ]
    store = Store(":memory:")
    service = ProvenanceService(store, policies)
    worker = JobWorker(service)

    author = Actor("u-editor", {"author"})
    fact_checker = Actor("u-fact", {"fact_checker"})
    release_manager = Actor("u-rel", {"release_manager"})
    rights_officer = Actor("u-rights", {"rights_officer"})
    scope_all = {"channels": ["all"], "commercial": True}

    # 1. 登记素材：市井画面、民俗画面、AI 生成的中秋旋律
    market = service.register_asset(
        author, "video", "hash-market", "self_recorded",
        portrait_subjects=["vendor-a"], now=T0,
    )
    folk = service.register_asset(
        author, "video", "hash-folk", "self_recorded", now=at(minutes=1),
    )
    melody = service.register_asset(
        author, "audio", "hash-melody-ai", "ai_generated",
        # 生成参数仅作谱系记录，不构成版权证明
        generation_params={"model": "melody-ai", "prompt_digest": "sha256:中秋/山海/电子"},
        now=at(minutes=2),
    )

    # 2. 权利申报（只能本人）与核验（须他人中的核验角色）
    portrait = service.declare_rights(
        Actor("vendor-a"), market.asset_id, "portrait", "vendor-a", scope_all, now=at(minutes=3)
    )
    composition = service.declare_rights(
        Actor("u-editor"), melody.asset_id, "composition", "u-editor", scope_all, now=at(minutes=4)
    )
    service.verify_rights(fact_checker, portrait, now=at(minutes=5))
    service.verify_rights(fact_checker, composition, now=at(minutes=6))
    service.record_contribution(author, market.asset_id, "filming", now=at(minutes=7))
    service.record_contribution(author, melody.asset_id, "ai_generation", now=at(minutes=8))

    # 3. 剪辑方案：锁定曲目、双角色签署
    items = [
        {"kind": "video", "asset_ref": market.asset_id},
        {"kind": "video", "asset_ref": folk.asset_id},
        {"kind": "audio", "asset_ref": melody.asset_id},
        {"kind": "description", "text": "片尾限定：含 AI 生成旋律，禁止二次商用"},
    ]
    revision = service.create_revision(author, items, now=at(minutes=9))
    service.reserve_tracks(author, revision, [melody.asset_id], now=at(minutes=10))
    service.signoff(fact_checker, revision, "fact", now=at(minutes=11))
    service.signoff(release_manager, revision, "commercial", now=at(minutes=12))

    # 4. 渠道发布：电视渠道拒绝 AI 音乐，短视频渠道冻结快照
    try:
        service.post_release(release_manager, revision, "tv", now=at(minutes=13))
    except Exception as exc:  # noqa: BLE001 - 演示渠道限制拦截
        show("电视渠道发布被拒（渠道限制）", {"原因": str(exc)})
    release = service.post_release(
        release_manager, revision, "short-video",
        notices=[{"kind": "ai_disclosure", "text": "含 AI 生成旋律"}],
        now=at(minutes=14),
    )
    show("短视频渠道发布冻结", service.explain_release(release, now=at(minutes=15)))

    # 5. 来源撤回：在线版本生成范围明确的替换任务
    outcome = service.withdraw_source(
        rights_officer, melody.asset_id, "AI 旋律训练数据来源存疑",
        replacement_due_at=at(hours=24), now=at(hours=1),
    )
    show("撤回结果", outcome)

    # 6. 中断后重启：新的 worker 实例继续处理到期任务
    worker2 = JobWorker(service)
    processed = worker2.run_pending(at(hours=25))
    show("中断恢复后处理的任务", {"processed": processed})
    show("替换逾期后的许可状态", service.explain_release(release, now=at(hours=26)))

    store.close()


if __name__ == "__main__":
    main()
