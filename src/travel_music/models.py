"""素材谱系服务的领域模型：角色、状态、渠道策略与错误类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class ServiceError(Exception):
    """服务层基础错误。"""


class NotFoundError(ServiceError):
    """目标对象不存在。"""


class PermissionDenied(ServiceError):
    """违反角色或职责分离约束。"""


class StateError(ServiceError):
    """聚合当前状态不允许该操作。"""


class ConflictError(ServiceError):
    """并发占用冲突；抛出前事务已整体回滚，不会留下部分锁。"""


class ContractViolation(ServiceError):
    """服务生成的事件未通过基础契约校验。"""

    def __init__(self, issues: Iterable[object]) -> None:
        self.issues = list(issues)
        detail = "; ".join(f"{issue.field}:{issue.code}" for issue in self.issues)
        super().__init__(f"事件未通过契约校验: {detail}")


# ---- 角色 ----
ROLE_AUTHOR = "author"
ROLE_FACT_CHECKER = "fact_checker"
ROLE_RELEASE_MANAGER = "release_manager"
ROLE_RIGHTS_OFFICER = "rights_officer"

# ---- 素材类别 ----
ASSET_KIND_VIDEO = "video"
ASSET_KIND_AUDIO = "audio"
ASSET_KINDS = (ASSET_KIND_VIDEO, ASSET_KIND_AUDIO)

# ---- 素材来源 ----
SOURCE_SELF_RECORDED = "self_recorded"  # 自拍素材
SOURCE_COMMISSIONED = "commissioned"  # 委托作曲
SOURCE_PUBLIC_SAMPLE = "public_sample"  # 公开采样
SOURCE_AI_GENERATED = "ai_generated"  # AI 生成
SOURCE_LICENSED_LIBRARY = "licensed_library"  # 版权曲库
SOURCE_KINDS = (
    SOURCE_SELF_RECORDED,
    SOURCE_COMMISSIONED,
    SOURCE_PUBLIC_SAMPLE,
    SOURCE_AI_GENERATED,
    SOURCE_LICENSED_LIBRARY,
)

# ---- 素材状态 ----
ASSET_STATE_REGISTERED = "registered"
ASSET_STATE_QUARANTINED = "quarantined"
ASSET_STATE_REJECTED = "rejected"
ASSET_STATE_WITHDRAWN = "withdrawn"

# ---- 授权 ----
GRANT_KIND_COMPOSITION = "composition"  # 词曲授权
GRANT_KIND_PORTRAIT = "portrait"  # 肖像授权
GRANT_KINDS = (GRANT_KIND_COMPOSITION, GRANT_KIND_PORTRAIT)

GRANT_STATE_DECLARED = "declared"
GRANT_STATE_VERIFIED = "verified"

# ---- 剪辑版本 ----
REVISION_STATE_DRAFT = "draft"
REVISION_STATE_READY = "ready"  # 事实核验与商业发布双签署完成
REVISION_STATE_RELEASED = "released"
REVISION_STATE_BLOCKED = "blocked"
REVISION_STATE_ABANDONED = "abandoned"
REVISION_ACTIVE_STATES = (REVISION_STATE_DRAFT, REVISION_STATE_READY)

# ---- 渠道发布 ----
RELEASE_STATE_LIVE = "live"
RELEASE_STATE_ENDED = "ended"
RELEASE_STATE_SUSPENDED = "suspended"

# ---- 替换任务 ----
TASK_STATE_OPEN = "open"
TASK_STATE_DONE = "done"

# ---- 后台任务 ----
JOB_STATUS_PENDING = "pending"
JOB_STATUS_CLAIMED = "claimed"
JOB_STATUS_DONE = "done"

JOB_ENFORCE_REPLACEMENT = "enforce_replacement"
JOB_TAKEDOWN_ENFORCE = "takedown_enforce"
JOB_CORRECTION_PROPAGATION = "correction_propagation"

# ---- 签署与条目 ----
SIGNOFF_FACT = "fact"
SIGNOFF_COMMERCIAL = "commercial"

ITEM_KIND_VIDEO = "video"
ITEM_KIND_AUDIO = "audio"
ITEM_KIND_DESCRIPTION = "description"
ITEM_KINDS = (ITEM_KIND_VIDEO, ITEM_KIND_AUDIO, ITEM_KIND_DESCRIPTION)

CONTRIBUTION_ROLES = (
    "copywriting",  # 文案
    "editing",  # 剪辑
    "ai_generation",  # AI 工作
    "composition",  # 作曲
    "filming",  # 拍摄
    "performance",  # 演唱/演奏
)


@dataclass(frozen=True)
class Actor:
    """服务调用者；角色集合决定可执行的操作。"""

    actor_id: str
    roles: frozenset[str]

    def __init__(self, actor_id: str, roles: Iterable[str] = ()) -> None:
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "roles", frozenset(roles))

    def has(self, role: str) -> bool:
        return role in self.roles


@dataclass(frozen=True)
class ChannelPolicy:
    """渠道限制：发布时逐项校验。"""

    channel: str
    allow_ai_music: bool = True
    requires_portrait_grant: bool = True
    commercial: bool = True


@dataclass(frozen=True)
class RegistrationResult:
    """素材注册结果；reused=True 表示指纹重复，沿用原决定。"""

    asset_id: str
    state: str
    reused: bool
