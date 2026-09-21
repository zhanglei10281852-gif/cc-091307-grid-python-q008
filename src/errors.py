"""调解协作服务的领域异常定义。"""


class DomainError(Exception):
    """所有领域错误的基类。"""


class NotFoundError(DomainError):
    """请求的实体不存在。"""


class StateTransitionError(DomainError):
    """案件状态不允许执行该操作（违反规定的状态流转路径）。"""


class AuthorizationError(DomainError):
    """当前操作者没有执行该操作或查看该数据的权限。"""


class VersionConflictError(DomainError):
    """基于过期版本编辑，发生并发修改冲突。"""

    def __init__(self, message: str, expected_version: int, actual_version: int):
        super().__init__(message)
        self.expected_version = expected_version
        self.actual_version = actual_version


class ValidationError(DomainError):
    """输入参数或业务规则校验失败。"""
