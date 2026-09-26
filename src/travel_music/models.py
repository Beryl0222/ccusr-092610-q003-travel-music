"""素材谱系服务的领域枚举与错误类型。"""

from __future__ import annotations

from enum import Enum


class AssetKind(str, Enum):
    FOOTAGE = "footage"  # 拍摄画面
    MUSIC = "music"  # 音乐
    COPY = "copy"  # 说明文案


class SourceKind(str, Enum):
    COMMISSIONED = "commissioned"  # 委托作曲
    PUBLIC_SAMPLE = "public_sample"  # 公开采样
    AI_GENERATED = "ai_generated"  # AI 临时生成
    SELF_SHOT = "self_shot"  # 自拍素材


class GrantKind(str, Enum):
    COMPOSITION = "composition"  # 词曲授权
    PORTRAIT = "portrait"  # 肖像授权


class AssetStatus(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    WITHDRAWN = "withdrawn"


class GrantStatus(str, Enum):
    DECLARED = "declared"
    CONFIRMED = "confirmed"


class RevisionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"  # 事实核验与商业发布双签署完成
    BLOCKED = "blocked"  # 来源撤回，未发布版本被阻止


class ReleaseStatus(str, Enum):
    LIVE = "live"
    REPLACED = "replaced"
    TAKEN_DOWN = "taken_down"


class TaskStatus(str, Enum):
    OPEN = "open"
    DONE = "done"


class JobStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"


class LineageError(Exception):
    """服务层业务错误基类。"""


class NotFound(LineageError):
    """引用的对象不存在。"""


class PermissionDenied(LineageError):
    """角色或人员不满足职责分离要求。"""


class StateConflict(LineageError):
    """当前状态不允许该操作。"""


class LockConflict(StateConflict):
    """曲目已被其他剪辑方案占用。"""
