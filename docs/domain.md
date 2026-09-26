# 领域约定

报道描述年轻人假期奔赴山海、用车票串联沿途风光，国风与电子音乐结合并借助 AI 谱写中秋旋律，文案、剪辑和 AI 工作由不同参与者完成。

聚合对象包括`media_asset`、`rights_grant`、`edit_revision`、`channel_release`。事件类型包括`ASSET_REGISTERED`、`RIGHTS_DECLARED`、`REVISION_FROZEN`、`RELEASE_POSTED`、`TAKEDOWN_OPENED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ASSET_REGISTERED`：载荷还需包含 `content_hash`, `source_kind`。
- `RIGHTS_DECLARED`：载荷还需包含 `grantor_ref`, `scope`。
- `RELEASE_POSTED`：载荷还需包含 `channel`, `revision_ref`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
