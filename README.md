# 山河漫游音乐素材谱系

报道描述年轻人假期奔赴山海、用车票串联沿途风光，国风与电子音乐结合并借助 AI 谱写中秋旋律，文案、剪辑和 AI 工作由不同参与者完成。本仓库记录拍摄片段、音乐来源、生成参数摘要、词曲与肖像授权、创作者贡献、剪辑版本、渠道限制和下架通知，支撑素材谱系查询与合规发行。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/travel_music/contracts.py`：事件信封基础校验。
- `src/travel_music/models.py`：领域枚举与错误类型。
- `src/travel_music/store.py`：SQLite 持久化（事件日志、业务状态、后台任务）。
- `src/travel_music/service.py`：谱系服务（业务幂等、冲突隔离、状态推进）。
- `src/travel_music/cli.py`：命令行校验入口。
- `tests/`：契约测试与服务层规则测试。
- `docs/domain.md`：领域对象、事件语义与服务层保证。

## 服务层规则

- **指纹幂等**：重复提交相同素材指纹沿用原决定；指纹相同但来源或授权范围变化时，新登记进入隔离审查。
- **职责分离**：作者只能申报自己的创作；权利确认须由权利人本人或授权专员完成；事实核验与商业发布须由不同角色、不同人员签署。
- **发布冻结**：每个渠道发布时冻结实际采用的画面、声音和说明及当时许可状态，快照不可改写。
- **来源撤回**：只阻止未发布版本；对在线版本生成范围明确的替换任务（渠道、片段、期限），并调度更正传播与下架期限任务；已结束活动保留当时证据。
- **曲目原子锁**：多个剪辑方案占用同一首曲目时，靠数据库事务与主键冲突保证只有一个方案持有锁。
- **可解释**：`explain_cut(ref)` 从一段成片（发布或剪辑版本）解释素材来源、贡献者、授权范围与许可状态；生成参数摘要仅用于溯源，不构成版权证明。
- **断点续跑**：下架期限与更正传播是持久化后台任务，进程中断后重开数据库调用 `run_due_jobs(now)` 即可继续。

## 快速开始

```python
import json
from travel_music import LineageService, Store

schema = json.loads(open("contracts/domain.schema.json", encoding="utf-8").read())
svc = LineageService(Store("lineage.db"), schema)

svc.register_asset(event_id="e1", asset_id="music-1", kind="music",
                   content_hash="sha256:...", source_kind="ai_generated",
                   declared_by="ai-ops", occurred_at="2026-09-26T10:00:00+08:00",
                   generation_params={"model": "melody-x", "seed": 42})
svc.declare_grant(event_id="e2", grant_id="g1", asset_id="music-1",
                  grant_kind="composition", grantor_ref="composer",
                  scope={"channels": ["douyin"]}, declared_by="composer",
                  occurred_at="2026-09-26T10:01:00+08:00")
svc.confirm_grant(event_id="e3", grant_id="g1", confirmed_by="composer",
                  confirmer_roles=[], occurred_at="2026-09-26T10:02:00+08:00")
svc.create_revision(event_id="e4", revision_id="rev-1", created_by="editor",
                    items=[{"asset_id": "music-1", "usage": "audio", "segment": "00:00-00:30"}],
                    occurred_at="2026-09-26T10:03:00+08:00")
svc.sign_revision(event_id="e5", revision_id="rev-1", signer="checker-1",
                  signer_roles=["fact_checker"], sign_kind="fact",
                  occurred_at="2026-09-26T10:04:00+08:00")
svc.sign_revision(event_id="e6", revision_id="rev-1", signer="approver-1",
                  signer_roles=["commercial_approver"], sign_kind="commercial",
                  occurred_at="2026-09-26T10:05:00+08:00")
svc.post_release(event_id="e7", release_id="rel-1", revision_id="rev-1",
                 channel="douyin", posted_by="publisher",
                 occurred_at="2026-09-26T10:06:00+08:00")
print(svc.explain_cut("rel-1")["overall_license_status"])  # clear
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m travel_music.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
