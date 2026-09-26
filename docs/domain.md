# 领域约定

报道描述年轻人假期奔赴山海、用车票串联沿途风光，国风与电子音乐结合并借助 AI 谱写中秋旋律，文案、剪辑和 AI 工作由不同参与者完成。

聚合对象包括`media_asset`、`rights_grant`、`edit_revision`、`channel_release`、`replacement_task`、`takedown_notice`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ASSET_REGISTERED`：载荷还需包含 `content_hash`, `source_kind`。
- `RIGHTS_DECLARED`：载荷还需包含 `grantor_ref`, `scope`。
- `RELEASE_POSTED`：载荷还需包含 `channel`, `revision_ref`；服务层发布时另附 `frozen_items`, `notices` 作为冻结快照。
- `TAKEDOWN_OPENED`：载荷还需包含 `target_kind`, `target_ref`, `reason`, `due_at`, `opened_by`。

服务层新增事件（详见 `docs/service.md`）：

- 素材谱系：`ASSET_REGISTRATION_DEDUPED`, `ASSET_QUARANTINED`, `QUARANTINE_RESOLVED`, `SOURCE_WITHDRAWN`。
- 权利与贡献：`RIGHTS_VERIFIED`, `CONTRIBUTION_RECORDED`。
- 剪辑版本：`REVISION_CREATED`, `REVISION_SIGNOFF`, `REVISION_FROZEN`, `REVISION_BLOCKED`, `REVISION_ABANDONED`, `TRACKS_RESERVED`, `TRACKS_RELEASED`。
- 渠道发布：`RELEASE_ENDED`, `RELEASE_SUSPENDED`。
- 撤回与更正：`REPLACEMENT_TASK_OPENED`, `REPLACEMENT_TASK_CLOSED`, `TAKEDOWN_ENFORCED`, `CORRECTION_ISSUED`, `CORRECTION_PROPAGATED`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责（`src/travel_music/service.py`）；本契约只定义可稳定交换的基础事实。契约演进保持向后兼容：既有事件类型的必填载荷只减不增。
