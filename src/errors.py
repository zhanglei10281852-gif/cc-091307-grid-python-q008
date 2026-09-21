"""调解协作服务的领域异常。"""

from __future__ import annotations


class MediationError(Exception):
    """调解服务基础异常。"""


class NotFoundError(MediationError):
    """请求的案件、当事人、证据等对象不存在。"""


class ValidationError(MediationError):
    """输入参数不合法。"""


class InvalidStateTransition(MediationError):
    """案件当前状态不允许执行该操作。"""


class PermissionDeniedError(MediationError):
    """当前角色无权执行该操作或查看该内容。"""


class VersionConflictError(MediationError):
    """会谈纪要版本冲突：提交时基于的版本已过期。"""

    def __init__(self, minutes_id: str, expected: int, current: int):
        self.minutes_id = minutes_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"会谈纪要 {minutes_id} 版本冲突：提交基于 v{expected}，"
            f"当前已是 v{current}，请刷新后重试"
        )
