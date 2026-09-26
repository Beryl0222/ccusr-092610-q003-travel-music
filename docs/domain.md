# 领域约定

报道描述年轻人假期奔赴山海、用车票串联沿途风光，国风与电子音乐结合并借助 AI 谱写中秋旋律，文案、剪辑和 AI 工作由不同参与者完成。

聚合对象包括`media_asset`、`contribution`、`rights_grant`、`edit_revision`、`track_lock`、`channel_release`、`takedown_notice`、`replacement_task`、`campaign`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ASSET_REGISTERED`：载荷还需包含 `content_hash`, `source_kind`。
- `CONTRIBUTION_DECLARED`：载荷还需包含 `creator_id`, `role`, `asset_ref`。
- `RIGHTS_DECLARED`：载荷还需包含 `grantor_ref`, `scope`。
- `RIGHTS_CONFIRMED`：载荷还需包含 `grant_ref`, `confirmed_by`。
- `REVISION_CREATED`：载荷还需包含 `items`。
- `REVISION_SIGNED`：载荷还需包含 `sign_kind`, `signer`。
- `TRACK_LOCKED`：载荷还需包含 `revision_ref`, `track_ref`。
- `REVISION_FROZEN`：载荷还需包含 `frozen_items`, `channel`。
- `RELEASE_POSTED`：载荷还需包含 `channel`, `revision_ref`。
- `SOURCE_WITHDRAWN`：载荷还需包含 `reason`。
- `TAKEDOWN_OPENED`：载荷还需包含 `asset_ref`, `reason`, `deadline_at`。
- `TAKEDOWN_ENFORCED`：载荷还需包含 `takedown_ref`。
- `REPLACEMENT_TASK_OPENED`：载荷还需包含 `release_ref`, `asset_ref`, `scope`。
- `CAMPAIGN_CLOSED`：载荷还需包含 `evidence_ref`。
- `CORRECTION_PROPAGATED`：载荷还需包含 `release_ref`, `notice`。

## 服务层语义

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本层只定义可稳定交换的基础事实。服务层（`travel_music.service`）在此基础上保证：

- 重复提交相同素材指纹沿用原决定；指纹相同但来源或授权范围变化时隔离审查。
- 作者只能申报自己的创作，不能替他人确认权利；事实核验与商业发布须由不同角色、不同人员签署。
- 渠道发布时冻结实际采用的画面、声音和说明；来源撤回只阻止未发布版本，对在线版本生成范围明确的替换任务；已结束活动保留当时证据。
- 多个剪辑方案占用同一首曲目时原子锁定；生成参数摘要仅用于溯源，不构成版权证明。
