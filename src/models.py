"""调解协作服务的领域模型。

包含案件状态机、证据状态、方案确认、共享授权等核心实体的定义。
所有实体均为不可变语义友好的 dataclass，并提供 ``to_dict`` 便于接口序列化。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Optional


class CaseStatus(str, enum.Enum):
    """案件状态。状态只能沿 ``ALLOWED_TRANSITIONS`` 规定的路径变化。"""

    PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT"  # 待分派调解员
    ASSIGNED = "ASSIGNED"                      # 已分派，调解进行中
    PROPOSAL_PENDING = "PROPOSAL_PENDING"      # 方案已提出，待双方确认
    AGREED = "AGREED"                          # 双方已确认方案，达成一致
    SUSPENDED = "SUSPENDED"                    # 暂缓
    CLOSED = "CLOSED"                          # 结案（终态）


#: 规定的状态流转路径。SUSPENDED 的恢复目标由案件上记录的 status_before_suspend 决定，
#: 因此恢复（resume）不经过该表，而是校验“暂缓前状态”后还原。
ALLOWED_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.PENDING_ASSIGNMENT: frozenset({CaseStatus.ASSIGNED}),
    CaseStatus.ASSIGNED: frozenset({
        CaseStatus.PROPOSAL_PENDING,
        CaseStatus.SUSPENDED,
    }),
    CaseStatus.PROPOSAL_PENDING: frozenset({
        CaseStatus.AGREED,           # 双方均确认方案
        CaseStatus.ASSIGNED,         # 任一方拒绝方案，回到调解中
        CaseStatus.SUSPENDED,
    }),
    CaseStatus.AGREED: frozenset({CaseStatus.CLOSED}),
    CaseStatus.SUSPENDED: frozenset({CaseStatus.CLOSED}),  # 恢复走 resume 专用逻辑
    CaseStatus.CLOSED: frozenset(),
}

#: 允许被暂缓（以及暂缓恢复）的业务状态。
SUSPENDABLE_STATUSES = frozenset({CaseStatus.ASSIGNED, CaseStatus.PROPOSAL_PENDING})


class Resolution(str, enum.Enum):
    """结案方式。"""

    AGREEMENT = "AGREEMENT"        # 双方达成一致后结案
    TERMINATED = "TERMINATED"      # 调解终止（暂缓后不再继续）


class EvidenceStatus(str, enum.Enum):
    """证据状态。状态可变，但原文与提交时间一经写入不得修改。"""

    SUBMITTED = "SUBMITTED"                    # 已提交
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # 待核验
    VERIFIED = "VERIFIED"                      # 已核验
    WITHDRAWN = "WITHDRAWN"                    # 已撤回（记录保留，不计入有效证据）


class ProposalStatus(str, enum.Enum):
    PENDING = "PENDING"        # 待双方确认
    AGREED = "AGREED"          # 双方均确认
    REJECTED = "REJECTED"      # 任一方拒绝
    SUPERSEDED = "SUPERSEDED"  # 被更新的方案取代


class Decision(str, enum.Enum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class GrantStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class IssueStatus(str, enum.Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class EventType(str, enum.Enum):
    """时间线事件类型。"""

    CASE_CREATED = "CASE_CREATED"
    MEDIATOR_ASSIGNED = "MEDIATOR_ASSIGNED"
    ISSUE_ADDED = "ISSUE_ADDED"
    EVIDENCE_SUBMITTED = "EVIDENCE_SUBMITTED"
    EVIDENCE_RESUBMITTED = "EVIDENCE_RESUBMITTED"  # 重复上传，未产生副本
    EVIDENCE_STATUS_CHANGED = "EVIDENCE_STATUS_CHANGED"
    MINUTE_CREATED = "MINUTE_CREATED"
    MINUTE_UPDATED = "MINUTE_UPDATED"
    PROPOSAL_SUBMITTED = "PROPOSAL_SUBMITTED"
    PROPOSAL_CONFIRMED = "PROPOSAL_CONFIRMED"
    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
    PROPOSAL_AGREED = "PROPOSAL_AGREED"  # 双方均已确认
    CASE_SUSPENDED = "CASE_SUSPENDED"
    CASE_RESUMED = "CASE_RESUMED"
    CASE_CLOSED = "CASE_CLOSED"
    SHARING_GRANTED = "SHARING_GRANTED"
    SHARING_REVOKED = "SHARING_REVOKED"


@dataclass
class Party:
    """当事人（居民）。contact 为隐私信息，仅本人与调解员可见。"""

    id: str
    name: str
    contact: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Case:
    id: str
    title: str
    description: str
    category: str
    party_a_id: str
    party_b_id: str
    status: CaseStatus
    mediator_id: Optional[str] = None
    status_before_suspend: Optional[CaseStatus] = None
    resolution: Optional[Resolution] = None
    close_reason: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    status_entered_at: str = ""
    closed_at: Optional[str] = None

    def party_ids(self) -> tuple[str, str]:
        return (self.party_a_id, self.party_b_id)

    def is_party(self, party_id: str) -> bool:
        return party_id in (self.party_a_id, self.party_b_id)

    def other_party(self, party_id: str) -> str:
        if party_id == self.party_a_id:
            return self.party_b_id
        if party_id == self.party_b_id:
            return self.party_a_id
        raise KeyError(party_id)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        data["resolution"] = self.resolution.value if self.resolution else None
        data["status_before_suspend"] = (
            self.status_before_suspend.value if self.status_before_suspend else None
        )
        return data


@dataclass
class Issue:
    """议题：双方争议的具体事项。"""

    id: str
    case_id: str
    title: str
    description: str
    raised_by: str
    status: IssueStatus = IssueStatus.OPEN
    created_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Evidence:
    """证据。content 与 submitted_at 为不可变留痕字段，任何状态变更不得改写。"""

    id: str
    case_id: str
    submitted_by: str
    content: str
    private_fields: dict = field(default_factory=dict)
    content_hash: str = ""
    status: EvidenceStatus = EvidenceStatus.SUBMITTED
    submitted_at: str = ""
    status_updated_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class EvidenceView:
    """按查看者权限裁剪后的证据视图。

    隐私字段未获双方授权时，``private_fields`` 为空且 ``private_fields_redacted`` 为 True。
    """

    id: str
    case_id: str
    submitted_by: str
    content: str
    status: EvidenceStatus
    submitted_at: str
    status_updated_at: str
    private_fields: dict = field(default_factory=dict)
    private_fields_redacted: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Minute:
    """会谈纪要。version 用于乐观锁：编辑必须基于当前版本，否则报冲突。"""

    id: str
    case_id: str
    title: str
    content: str
    version: int
    created_by: str
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MinuteVersion:
    """纪要历史版本留痕。"""

    minute_id: str
    version: int
    content: str
    edited_by: str
    edited_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Proposal:
    """调解方案。confirmations 记录每位当事人的确认/拒绝决定。"""

    id: str
    case_id: str
    content: str
    proposed_by: str
    status: ProposalStatus = ProposalStatus.PENDING
    confirmations: dict[str, Decision] = field(default_factory=dict)
    created_at: str = ""
    resolved_at: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        data["confirmations"] = {k: v.value for k, v in self.confirmations.items()}
        return data


@dataclass
class ShareGrant:
    """隐私字段共享授权。仅当双方互相授权（双向均存在有效授权）时，
    另一方才能看到证据的隐私字段。"""

    id: str
    case_id: str
    granted_by: str
    granted_to: str
    evidence_id: Optional[str] = None  # None 表示案件级授权
    status: GrantStatus = GrantStatus.ACTIVE
    created_at: str = ""
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class TimelineEvent:
    id: str
    case_id: str
    event_type: EventType
    actor: str
    detail: dict = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["event_type"] = self.event_type.value
        return data
