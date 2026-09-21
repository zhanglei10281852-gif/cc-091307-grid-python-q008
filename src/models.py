"""领域模型：案件状态机、证据状态、方案状态与时限配置。"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import timedelta


class CaseStatus(str, enum.Enum):
    """案件状态。"""

    INTAKE = "INTAKE"  # 已立案，待登记当事人 / 分派调解员
    ASSIGNED = "ASSIGNED"  # 已分派调解员
    IN_MEDIATION = "IN_MEDIATION"  # 调解中
    PROPOSAL_REVIEW = "PROPOSAL_REVIEW"  # 调解方案待双方确认
    RESOLVED = "RESOLVED"  # 双方已确认方案
    SUSPENDED = "SUSPENDED"  # 暂缓（记录暂缓前状态，恢复时原路返回）
    CLOSED = "CLOSED"  # 已结案（终态）


# 允许的状态迁移路径；暂缓恢复时还会额外校验目标状态确为暂缓前状态。
ALLOWED_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.INTAKE: frozenset({CaseStatus.ASSIGNED}),
    CaseStatus.ASSIGNED: frozenset({CaseStatus.IN_MEDIATION, CaseStatus.SUSPENDED}),
    CaseStatus.IN_MEDIATION: frozenset(
        {CaseStatus.PROPOSAL_REVIEW, CaseStatus.SUSPENDED, CaseStatus.CLOSED}
    ),
    CaseStatus.PROPOSAL_REVIEW: frozenset(
        {CaseStatus.RESOLVED, CaseStatus.IN_MEDIATION, CaseStatus.SUSPENDED}
    ),
    CaseStatus.RESOLVED: frozenset({CaseStatus.CLOSED}),
    CaseStatus.SUSPENDED: frozenset(
        {
            CaseStatus.ASSIGNED,
            CaseStatus.IN_MEDIATION,
            CaseStatus.PROPOSAL_REVIEW,
            CaseStatus.CLOSED,
        }
    ),
    CaseStatus.CLOSED: frozenset(),
}


class EvidenceStatus(str, enum.Enum):
    """证据状态。撤回与待核验只是标记，原文与提交时间始终保留。"""

    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # 待核验
    VERIFIED = "VERIFIED"  # 已核验
    WITHDRAWN = "WITHDRAWN"  # 已撤回


class ProposalStatus(str, enum.Enum):
    """调解方案状态。"""

    PENDING = "PENDING"  # 待双方确认
    CONFIRMED = "CONFIRMED"  # 双方均已确认
    REJECTED = "REJECTED"  # 任一方拒绝


@dataclass(frozen=True)
class SlaSettings:
    """调解员工作台时限配置，用于计算逾期原因。"""

    assign: timedelta = timedelta(hours=48)  # 立案 -> 分派调解员
    first_session: timedelta = timedelta(days=7)  # 分派 -> 开始调解
    proposal_confirm: timedelta = timedelta(days=7)  # 方案提出 -> 双方确认
    suspension: timedelta = timedelta(days=30)  # 暂缓最长时长
    evidence_verify: timedelta = timedelta(days=5)  # 证据待核验时长
