"""社区矛盾调解协作服务。

把当事人、议题、证据和会谈纪要组织成案件，支持：

- 分派调解员、提出方案、双方确认、暂缓与结案（状态机强制规定路径）
- 证据撤回 / 待核验标记（原文与提交时间永久保留），重复上传不产生副本
- 隐私字段共享：未经双方互相授权，另一方不可见
- 会谈纪要乐观锁版本控制，保留全部历史版本
- 案件时间线、授权共享、结果查询、调解员下一步动作与逾期原因
- SQLite 写穿持久化，重启后共享授权与会谈版本保持一致
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .errors import (
    AuthorizationError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
    VersionConflictError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    SUSPENDABLE_STATUSES,
    Case,
    CaseStatus,
    Decision,
    Evidence,
    EvidenceStatus,
    EvidenceView,
    EventType,
    GrantStatus,
    Issue,
    Minute,
    MinuteVersion,
    Party,
    Proposal,
    ProposalStatus,
    Resolution,
    ShareGrant,
    TimelineEvent,
)
from .storage import Database

#: 证据状态流转路径（WITHDRAWN 为终态，记录永久保留）。
_EVIDENCE_TRANSITIONS: dict[EvidenceStatus, frozenset[EvidenceStatus]] = {
    EvidenceStatus.SUBMITTED: frozenset({
        EvidenceStatus.PENDING_VERIFICATION,
        EvidenceStatus.VERIFIED,
        EvidenceStatus.WITHDRAWN,
    }),
    EvidenceStatus.PENDING_VERIFICATION: frozenset({
        EvidenceStatus.SUBMITTED,
        EvidenceStatus.VERIFIED,
        EvidenceStatus.WITHDRAWN,
    }),
    EvidenceStatus.VERIFIED: frozenset({
        EvidenceStatus.PENDING_VERIFICATION,
        EvidenceStatus.WITHDRAWN,
    }),
    EvidenceStatus.WITHDRAWN: frozenset(),
}

#: 各状态默认处理时限，超过即视为逾期。可在构造时覆盖。
DEFAULT_SLA: dict[CaseStatus, timedelta] = {
    CaseStatus.PENDING_ASSIGNMENT: timedelta(days=2),
    CaseStatus.ASSIGNED: timedelta(days=7),
    CaseStatus.PROPOSAL_PENDING: timedelta(days=5),
    CaseStatus.AGREED: timedelta(days=3),
    CaseStatus.SUSPENDED: timedelta(days=30),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _to_iso(value: datetime | str | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


class MediationService:
    """调解协作领域服务入口。

    :param db_path: SQLite 数据库路径，默认 ``:memory:``。
        使用文件路径时，重启进程后用同一路径重新实例化即可恢复全部状态。
    :param now_fn: 时钟函数（返回 datetime），测试时可注入以控制时间。
    :param sla: 各状态处理时限，缺省使用 ``DEFAULT_SLA``。
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        now_fn: Callable[[], datetime] = _utcnow,
        sla: Optional[dict[CaseStatus, timedelta]] = None,
    ):
        self._db = Database(db_path)
        self._now_fn = now_fn
        self._sla = dict(DEFAULT_SLA if sla is None else sla)
        self._lock = threading.RLock()
        self.ready = True

    def close(self) -> None:
        self._db.close()
        self.ready = False

    # ------------------------------------------------------------- 内部工具
    def _now(self) -> datetime:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _require_case(self, case_id: str) -> Case:
        case = self._db.get_case(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        return case

    def _require_party(self, party_id: str) -> Party:
        party = self._db.get_party(party_id)
        if party is None:
            raise NotFoundError(f"当事人不存在: {party_id}")
        return party

    def _require_case_party(self, case: Case, party_id: str) -> None:
        if not case.is_party(party_id):
            raise AuthorizationError(f"操作者 {party_id} 不是案件 {case.id} 的当事人")

    def _require_case_member(self, case: Case, actor_id: str) -> None:
        """当事人或本案调解员。"""
        if not case.is_party(actor_id) and actor_id != case.mediator_id:
            raise AuthorizationError(f"操作者 {actor_id} 与案件 {case.id} 无关")

    def _require_mediator(self, case: Case, actor_id: str) -> None:
        if case.mediator_id is None or actor_id != case.mediator_id:
            raise AuthorizationError(f"操作者 {actor_id} 不是案件 {case.id} 的调解员")

    def _require_not_closed(self, case: Case) -> None:
        if case.status == CaseStatus.CLOSED:
            raise StateTransitionError(f"案件 {case.id} 已结案，不能再修改")

    def _record_event(self, case_id: str, event_type: EventType, actor: str,
                      detail: Optional[dict] = None) -> None:
        self._db.insert_event(TimelineEvent(
            id=_new_id(), case_id=case_id, event_type=event_type, actor=actor,
            detail=detail or {}, created_at=self._now_iso(),
        ))

    def _transition(self, case: Case, target: CaseStatus, actor: str,
                    event_type: EventType, detail: Optional[dict] = None) -> None:
        """沿规定路径变更案件状态并留痕。"""
        allowed = ALLOWED_TRANSITIONS[case.status]
        if target not in allowed:
            raise StateTransitionError(
                f"案件 {case.id} 不允许从 {case.status.value} 变更为 {target.value}"
            )
        now_iso = self._now_iso()
        case.status = target
        case.status_entered_at = now_iso
        case.updated_at = now_iso
        self._db.update_case(case)
        self._record_event(case.id, event_type, actor, detail)

    # ----------------------------------------------------------- 当事人管理
    def register_party(self, name: str, contact: str) -> Party:
        """登记当事人。contact 为隐私信息，不向另一方展示。"""
        if not name or not name.strip():
            raise ValidationError("当事人姓名不能为空")
        with self._lock:
            party = Party(id=_new_id(), name=name.strip(), contact=contact or "")
            self._db.insert_party(party)
            return party

    def get_party(self, party_id: str) -> Party:
        return self._require_party(party_id)

    # ------------------------------------------------------------- 案件管理
    def create_case(self, title: str, description: str, category: str,
                    party_a_id: str, party_b_id: str,
                    created_by: str = "system") -> Case:
        """立案：组织双方当事人进入一个新案件，初始状态为待分派。"""
        if not title or not title.strip():
            raise ValidationError("案件标题不能为空")
        if party_a_id == party_b_id:
            raise ValidationError("案件双方不能是同一人")
        with self._lock:
            self._require_party(party_a_id)
            self._require_party(party_b_id)
            now_iso = self._now_iso()
            case = Case(
                id=_new_id(), title=title.strip(), description=description or "",
                category=category or "", party_a_id=party_a_id,
                party_b_id=party_b_id, status=CaseStatus.PENDING_ASSIGNMENT,
                created_at=now_iso, updated_at=now_iso, status_entered_at=now_iso,
            )
            self._db.insert_case(case)
            self._record_event(case.id, EventType.CASE_CREATED, created_by,
                               {"title": case.title, "category": case.category})
            return case

    def get_case(self, case_id: str) -> Case:
        return self._require_case(case_id)

    def assign_mediator(self, case_id: str, mediator_id: str,
                        assigned_by: str = "system") -> Case:
        """分派调解员：待分派 → 已分派。"""
        if not mediator_id:
            raise ValidationError("调解员不能为空")
        with self._lock:
            case = self._require_case(case_id)
            if case.mediator_id is not None:
                raise StateTransitionError(f"案件 {case_id} 已分派调解员")
            case.mediator_id = mediator_id
            self._transition(case, CaseStatus.ASSIGNED, assigned_by,
                             EventType.MEDIATOR_ASSIGNED,
                             {"mediator_id": mediator_id})
            return case

    # ----------------------------------------------------------------- 议题
    def add_issue(self, case_id: str, title: str, description: str,
                  raised_by: str) -> Issue:
        """登记议题（争议事项）。当事人或调解员可提出。"""
        if not title or not title.strip():
            raise ValidationError("议题标题不能为空")
        with self._lock:
            case = self._require_case(case_id)
            self._require_not_closed(case)
            self._require_case_member(case, raised_by)
            issue = Issue(id=_new_id(), case_id=case_id, title=title.strip(),
                          description=description or "", raised_by=raised_by,
                          created_at=self._now_iso())
            self._db.insert_issue(issue)
            self._record_event(case_id, EventType.ISSUE_ADDED, raised_by,
                               {"issue_id": issue.id, "title": issue.title})
            return issue

    def list_issues(self, case_id: str) -> list[Issue]:
        self._require_case(case_id)
        return self._db.list_issues(case_id)

    # ----------------------------------------------------------------- 证据
    @staticmethod
    def _evidence_hash(case_id: str, submitted_by: str, content: str,
                       private_fields: dict) -> str:
        payload = "\n".join([
            case_id, submitted_by, content,
            json.dumps(private_fields, ensure_ascii=False, sort_keys=True),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def submit_evidence(self, case_id: str, submitted_by: str, content: str,
                        private_fields: Optional[dict] = None
                        ) -> tuple[Evidence, bool]:
        """提交证据。返回 ``(证据, 是否新建)``。

        同一案件中同一提交者上传相同内容（含隐私字段）时按内容哈希去重，
        不产生副本，返回已存在的证据并留痕一次重复提交事件。
        """
        if not content or not content.strip():
            raise ValidationError("证据内容不能为空")
        private_fields = dict(private_fields or {})
        with self._lock:
            case = self._require_case(case_id)
            self._require_not_closed(case)
            self._require_case_member(case, submitted_by)
            content_hash = self._evidence_hash(case_id, submitted_by,
                                               content, private_fields)
            existing = self._db.get_evidence_by_hash(content_hash)
            if existing is not None:
                self._record_event(case_id, EventType.EVIDENCE_RESUBMITTED,
                                   submitted_by,
                                   {"evidence_id": existing.id,
                                    "note": "重复上传同一证据，未产生副本"})
                return existing, False
            now_iso = self._now_iso()
            evidence = Evidence(
                id=_new_id(), case_id=case_id, submitted_by=submitted_by,
                content=content, private_fields=private_fields,
                content_hash=content_hash, status=EvidenceStatus.SUBMITTED,
                submitted_at=now_iso, status_updated_at=now_iso,
            )
            self._db.insert_evidence(evidence)
            self._record_event(case_id, EventType.EVIDENCE_SUBMITTED,
                               submitted_by, {"evidence_id": evidence.id})
            return evidence, True

    def _change_evidence_status(self, case_id: str, evidence_id: str,
                                target: EvidenceStatus, actor: str,
                                reason: Optional[str]) -> Evidence:
        case = self._require_case(case_id)
        self._require_not_closed(case)
        evidence = self._db.get_evidence(evidence_id)
        if evidence is None or evidence.case_id != case_id:
            raise NotFoundError(f"证据不存在: {evidence_id}")
        allowed = _EVIDENCE_TRANSITIONS[evidence.status]
        if target not in allowed:
            raise StateTransitionError(
                f"证据 {evidence_id} 不允许从 {evidence.status.value} "
                f"变更为 {target.value}"
            )
        # 仅更新状态；原文 content 与 submitted_at 永不改写。
        self._db.update_evidence_status(evidence_id, target, self._now_iso())
        evidence.status = target
        evidence.status_updated_at = self._now_iso()
        self._record_event(case_id, EventType.EVIDENCE_STATUS_CHANGED, actor,
                           {"evidence_id": evidence_id,
                            "new_status": target.value,
                            "reason": reason or ""})
        return evidence

    def mark_evidence_pending_verification(self, case_id: str, evidence_id: str,
                                           marked_by: str,
                                           reason: Optional[str] = None) -> Evidence:
        """标记证据为待核验（仅调解员）。"""
        with self._lock:
            self._require_mediator(self._require_case(case_id), marked_by)
            return self._change_evidence_status(
                case_id, evidence_id, EvidenceStatus.PENDING_VERIFICATION,
                marked_by, reason)

    def verify_evidence(self, case_id: str, evidence_id: str,
                        verified_by: str) -> Evidence:
        """核验通过（仅调解员）。"""
        with self._lock:
            self._require_mediator(self._require_case(case_id), verified_by)
            return self._change_evidence_status(
                case_id, evidence_id, EvidenceStatus.VERIFIED,
                verified_by, None)

    def withdraw_evidence(self, case_id: str, evidence_id: str,
                          withdrawn_by: str,
                          reason: Optional[str] = None) -> Evidence:
        """撤回证据（提交人本人或调解员）。记录保留，不计入有效证据。"""
        with self._lock:
            case = self._require_case(case_id)
            evidence = self._db.get_evidence(evidence_id)
            if evidence is None or evidence.case_id != case_id:
                raise NotFoundError(f"证据不存在: {evidence_id}")
            if withdrawn_by != evidence.submitted_by:
                self._require_mediator(case, withdrawn_by)
            return self._change_evidence_status(
                case_id, evidence_id, EvidenceStatus.WITHDRAWN,
                withdrawn_by, reason)

    # ----------------------------------------------------------- 共享与隐私
    def grant_sharing(self, case_id: str, granted_by: str, granted_to: str,
                      evidence_id: Optional[str] = None,
                      expires_at: datetime | str | None = None) -> ShareGrant:
        """授权对方向自己开放隐私字段。

        隐私字段只有在双方互相授权（两个方向均存在有效授权）后才对另一方可见。
        重复授权同一范围时幂等返回已有授权。
        """
        with self._lock:
            case = self._require_case(case_id)
            self._require_not_closed(case)
            self._require_case_party(case, granted_by)
            self._require_case_party(case, granted_to)
            if granted_by == granted_to:
                raise ValidationError("不能授权给自己")
            if evidence_id is not None:
                evidence = self._db.get_evidence(evidence_id)
                if evidence is None or evidence.case_id != case_id:
                    raise NotFoundError(f"证据不存在: {evidence_id}")
            for grant in self._db.list_share_grants(case_id):
                if (grant.granted_by == granted_by
                        and grant.granted_to == granted_to
                        and grant.evidence_id == evidence_id
                        and grant.status == GrantStatus.ACTIVE):
                    return grant  # 幂等：已有有效授权
            grant = ShareGrant(
                id=_new_id(), case_id=case_id, granted_by=granted_by,
                granted_to=granted_to, evidence_id=evidence_id,
                created_at=self._now_iso(), expires_at=_to_iso(expires_at),
            )
            self._db.insert_share_grant(grant)
            self._record_event(case_id, EventType.SHARING_GRANTED, granted_by,
                               {"grant_id": grant.id, "granted_to": granted_to,
                                "evidence_id": evidence_id})
            return grant

    def revoke_sharing(self, case_id: str, grant_id: str,
                       revoked_by: str) -> ShareGrant:
        """撤销授权（授权人本人或调解员）。"""
        with self._lock:
            case = self._require_case(case_id)
            grant = self._db.get_share_grant(grant_id)
            if grant is None or grant.case_id != case_id:
                raise NotFoundError(f"授权不存在: {grant_id}")
            if grant.status != GrantStatus.ACTIVE:
                raise StateTransitionError(f"授权 {grant_id} 已撤销")
            if revoked_by != grant.granted_by:
                self._require_mediator(case, revoked_by)
            grant.status = GrantStatus.REVOKED
            grant.revoked_at = self._now_iso()
            self._db.update_share_grant(grant)
            self._record_event(case_id, EventType.SHARING_REVOKED, revoked_by,
                               {"grant_id": grant_id})
            return grant

    def list_share_grants(self, case_id: str) -> list[ShareGrant]:
        self._require_case(case_id)
        return self._db.list_share_grants(case_id)

    def _has_active_grant(self, case_id: str, evidence_id: str,
                          granted_by: str, granted_to: str,
                          now: datetime) -> bool:
        for grant in self._db.list_share_grants(case_id):
            if (grant.granted_by == granted_by
                    and grant.granted_to == granted_to
                    and grant.status == GrantStatus.ACTIVE
                    and (grant.evidence_id is None
                         or grant.evidence_id == evidence_id)
                    and (grant.expires_at is None
                         or datetime.fromisoformat(grant.expires_at) > now)):
                return True
        return False

    def can_view_private_fields(self, case_id: str, evidence_id: str,
                                viewer_id: str,
                                now: Optional[datetime] = None) -> bool:
        """判断查看者是否可见证据隐私字段。

        提交人本人与调解员始终可见；另一方仅在双方互相授权后可见。
        """
        case = self._require_case(case_id)
        evidence = self._db.get_evidence(evidence_id)
        if evidence is None or evidence.case_id != case_id:
            raise NotFoundError(f"证据不存在: {evidence_id}")
        if viewer_id == evidence.submitted_by or viewer_id == case.mediator_id:
            return True
        self._require_case_party(case, viewer_id)
        now = now or self._now()
        return (
            self._has_active_grant(case_id, evidence_id, evidence.submitted_by,
                                   viewer_id, now)
            and self._has_active_grant(case_id, evidence_id, viewer_id,
                                       evidence.submitted_by, now)
        )

    def _to_evidence_view(self, case: Case, evidence: Evidence,
                          viewer_id: str) -> EvidenceView:
        visible = self.can_view_private_fields(case.id, evidence.id, viewer_id)
        return EvidenceView(
            id=evidence.id, case_id=evidence.case_id,
            submitted_by=evidence.submitted_by, content=evidence.content,
            status=evidence.status, submitted_at=evidence.submitted_at,
            status_updated_at=evidence.status_updated_at,
            private_fields=dict(evidence.private_fields) if visible else {},
            private_fields_redacted=not visible,
        )

    def get_evidence(self, case_id: str, evidence_id: str,
                     viewer_id: str) -> EvidenceView:
        """按查看者权限获取证据视图；未获双方授权时隐私字段被隐藏。"""
        case = self._require_case(case_id)
        self._require_case_member(case, viewer_id)
        evidence = self._db.get_evidence(evidence_id)
        if evidence is None or evidence.case_id != case_id:
            raise NotFoundError(f"证据不存在: {evidence_id}")
        return self._to_evidence_view(case, evidence, viewer_id)

    def list_evidence(self, case_id: str, viewer_id: str) -> list[EvidenceView]:
        """列出案件全部证据（含已撤回，原文保留），按查看者权限裁剪。"""
        case = self._require_case(case_id)
        self._require_case_member(case, viewer_id)
        return [self._to_evidence_view(case, ev, viewer_id)
                for ev in self._db.list_evidence(case_id)]

    # ------------------------------------------------------------- 会谈纪要
    def create_minute(self, case_id: str, title: str, content: str,
                      created_by: str) -> Minute:
        """创建会谈纪要，初始版本为 1。"""
        if not title or not title.strip():
            raise ValidationError("纪要标题不能为空")
        with self._lock:
            case = self._require_case(case_id)
            self._require_not_closed(case)
            self._require_case_member(case, created_by)
            now_iso = self._now_iso()
            minute = Minute(id=_new_id(), case_id=case_id, title=title.strip(),
                            content=content or "", version=1,
                            created_by=created_by, created_at=now_iso,
                            updated_at=now_iso)
            self._db.insert_minute(minute)
            self._db.insert_minute_version(MinuteVersion(
                minute_id=minute.id, version=1, content=minute.content,
                edited_by=created_by, edited_at=now_iso))
            self._record_event(case_id, EventType.MINUTE_CREATED, created_by,
                               {"minute_id": minute.id, "title": minute.title})
            return minute

    def update_minute(self, case_id: str, minute_id: str, content: str,
                      base_version: int, edited_by: str) -> Minute:
        """编辑纪要。必须基于当前版本编辑，否则抛出 ``VersionConflictError``。"""
        with self._lock:
            case = self._require_case(case_id)
            self._require_not_closed(case)
            self._require_case_member(case, edited_by)
            minute = self._db.get_minute(minute_id)
            if minute is None or minute.case_id != case_id:
                raise NotFoundError(f"纪要不存在: {minute_id}")
            if minute.version != base_version:
                raise VersionConflictError(
                    f"纪要 {minute_id} 版本冲突：基于版本 {base_version}，"
                    f"当前版本 {minute.version}",
                    expected_version=base_version,
                    actual_version=minute.version,
                )
            now_iso = self._now_iso()
            minute.content = content
            minute.version += 1
            minute.updated_at = now_iso
            self._db.update_minute(minute)
            self._db.insert_minute_version(MinuteVersion(
                minute_id=minute.id, version=minute.version,
                content=content, edited_by=edited_by, edited_at=now_iso))
            self._record_event(case_id, EventType.MINUTE_UPDATED, edited_by,
                               {"minute_id": minute.id,
                                "version": minute.version})
            return minute

    def get_minute(self, case_id: str, minute_id: str) -> Minute:
        self._require_case(case_id)
        minute = self._db.get_minute(minute_id)
        if minute is None or minute.case_id != case_id:
            raise NotFoundError(f"纪要不存在: {minute_id}")
        return minute

    def list_minutes(self, case_id: str) -> list[Minute]:
        self._require_case(case_id)
        return self._db.list_minutes(case_id)

    def minute_history(self, case_id: str, minute_id: str) -> list[MinuteVersion]:
        """纪要全部历史版本（留痕）。"""
        self.get_minute(case_id, minute_id)
        return self._db.list_minute_versions(minute_id)

    # ------------------------------------------------------------- 调解流程
    def propose_solution(self, case_id: str, content: str,
                         proposed_by: str) -> Proposal:
        """调解员提出方案：已分派 → 方案待确认。"""
        if not content or not content.strip():
            raise ValidationError("方案内容不能为空")
        with self._lock:
            case = self._require_case(case_id)
            if case.status != CaseStatus.ASSIGNED:
                raise StateTransitionError(
                    f"案件 {case_id} 当前状态为 {case.status.value}，不能提出方案")
            self._require_mediator(case, proposed_by)
            proposal = Proposal(
                id=_new_id(), case_id=case_id, content=content.strip(),
                proposed_by=proposed_by, created_at=self._now_iso())
            self._db.insert_proposal(proposal)
            self._transition(case, CaseStatus.PROPOSAL_PENDING, proposed_by,
                             EventType.PROPOSAL_SUBMITTED,
                             {"proposal_id": proposal.id})
            return proposal

    def _active_proposal(self, case: Case, proposal_id: str) -> Proposal:
        proposal = self._db.get_proposal(proposal_id)
        if proposal is None or proposal.case_id != case.id:
            raise NotFoundError(f"方案不存在: {proposal_id}")
        if case.status != CaseStatus.PROPOSAL_PENDING:
            raise StateTransitionError(
                f"案件 {case.id} 当前状态为 {case.status.value}，不能确认方案")
        if proposal.status != ProposalStatus.PENDING:
            raise StateTransitionError(f"方案 {proposal_id} 已不在待确认状态")
        return proposal

    def confirm_proposal(self, case_id: str, proposal_id: str,
                         party_id: str) -> Proposal:
        """当事人确认方案。双方均确认后：方案待确认 → 已达成一致。

        重复确认幂等（返回当前方案，不产生重复事件）。
        """
        with self._lock:
            case = self._require_case(case_id)
            self._require_case_party(case, party_id)
            proposal = self._active_proposal(case, proposal_id)
            existing = proposal.confirmations.get(party_id)
            if existing == Decision.CONFIRMED:
                return proposal
            if existing == Decision.REJECTED:
                raise ValidationError(f"当事人 {party_id} 已拒绝该方案")
            proposal.confirmations[party_id] = Decision.CONFIRMED
            self._db.upsert_confirmation(proposal.id, party_id,
                                         Decision.CONFIRMED, self._now_iso())
            self._record_event(case_id, EventType.PROPOSAL_CONFIRMED, party_id,
                               {"proposal_id": proposal.id})
            if all(proposal.confirmations.get(pid) == Decision.CONFIRMED
                   for pid in case.party_ids()):
                proposal.status = ProposalStatus.AGREED
                proposal.resolved_at = self._now_iso()
                self._db.update_proposal(proposal)
                self._transition(case, CaseStatus.AGREED, party_id,
                                 EventType.PROPOSAL_AGREED,
                                 {"proposal_id": proposal.id})
            return proposal

    def reject_proposal(self, case_id: str, proposal_id: str, party_id: str,
                        reason: Optional[str] = None) -> Proposal:
        """任一方拒绝方案：方案待确认 → 已分派（重新调解）。"""
        with self._lock:
            case = self._require_case(case_id)
            self._require_case_party(case, party_id)
            proposal = self._active_proposal(case, proposal_id)
            existing = proposal.confirmations.get(party_id)
            if existing == Decision.REJECTED:
                return proposal
            if existing == Decision.CONFIRMED:
                raise ValidationError(f"当事人 {party_id} 已确认该方案，不能改为拒绝")
            proposal.confirmations[party_id] = Decision.REJECTED
            self._db.upsert_confirmation(proposal.id, party_id,
                                         Decision.REJECTED, self._now_iso())
            proposal.status = ProposalStatus.REJECTED
            proposal.resolved_at = self._now_iso()
            self._db.update_proposal(proposal)
            self._transition(case, CaseStatus.ASSIGNED, party_id,
                             EventType.PROPOSAL_REJECTED,
                             {"proposal_id": proposal.id,
                              "reason": reason or "",
                              "note": "方案被拒绝，回到调解中"})
            return proposal

    def suspend_case(self, case_id: str, suspended_by: str,
                     reason: Optional[str] = None) -> Case:
        """暂缓案件（调解员或当事人均可发起）。记录暂缓前状态以便恢复。"""
        with self._lock:
            case = self._require_case(case_id)
            self._require_case_member(case, suspended_by)
            if case.status not in SUSPENDABLE_STATUSES:
                raise StateTransitionError(
                    f"案件 {case_id} 当前状态为 {case.status.value}，不能暂缓")
            case.status_before_suspend = case.status
            self._transition(case, CaseStatus.SUSPENDED, suspended_by,
                             EventType.CASE_SUSPENDED,
                             {"reason": reason or "",
                              "suspended_from": case.status_before_suspend.value})
            return case

    def resume_case(self, case_id: str, resumed_by: str) -> Case:
        """恢复暂缓的案件（仅调解员），回到暂缓前状态。"""
        with self._lock:
            case = self._require_case(case_id)
            self._require_mediator(case, resumed_by)
            if case.status != CaseStatus.SUSPENDED:
                raise StateTransitionError(
                    f"案件 {case_id} 当前状态为 {case.status.value}，不能恢复")
            target = case.status_before_suspend
            if target is None:
                raise StateTransitionError(f"案件 {case_id} 缺少暂缓前状态，无法恢复")
            case.status_before_suspend = None
            now_iso = self._now_iso()
            case.status = target
            case.status_entered_at = now_iso
            case.updated_at = now_iso
            self._db.update_case(case)
            self._record_event(case_id, EventType.CASE_RESUMED, resumed_by,
                               {"resumed_to": target.value})
            return case

    def close_case(self, case_id: str, closed_by: str,
                   reason: Optional[str] = None) -> Case:
        """结案（仅调解员）。

        已达成一致 → 结案（AGREEMENT）；暂缓 → 结案（TERMINATED）。
        """
        with self._lock:
            case = self._require_case(case_id)
            self._require_mediator(case, closed_by)
            if case.status == CaseStatus.AGREED:
                resolution = Resolution.AGREEMENT
            elif case.status == CaseStatus.SUSPENDED:
                resolution = Resolution.TERMINATED
            else:
                raise StateTransitionError(
                    f"案件 {case_id} 当前状态为 {case.status.value}，不能结案；"
                    "只有已达成一致或暂缓中的案件可以结案")
            case.resolution = resolution
            case.close_reason = reason
            case.closed_at = self._now_iso()
            self._transition(case, CaseStatus.CLOSED, closed_by,
                             EventType.CASE_CLOSED,
                             {"resolution": resolution.value,
                              "reason": reason or ""})
            return case

    # ------------------------------------------------------------- 查询接口
    def get_timeline(self, case_id: str,
                     viewer_id: Optional[str] = None) -> list[TimelineEvent]:
        """案件时间线：全部关键动作按时间排序留痕。

        ``viewer_id`` 为 None 时表示系统内部查询；否则须为当事人或调解员。
        """
        case = self._require_case(case_id)
        if viewer_id is not None:
            self._require_case_member(case, viewer_id)
        return self._db.list_events(case_id)

    def get_case_result(self, case_id: str,
                        viewer_id: Optional[str] = None) -> dict:
        """结果查询：案件当前进展 / 结案结果汇总（不含隐私字段）。"""
        case = self._require_case(case_id)
        if viewer_id is not None:
            self._require_case_member(case, viewer_id)
        proposals = self._db.list_proposals(case_id)
        evidence_list = self._db.list_evidence(case_id)
        summary: dict[str, int] = {s.value: 0 for s in EvidenceStatus}
        for ev in evidence_list:
            summary[ev.status.value] += 1
        summary["total"] = len(evidence_list)
        latest = proposals[-1] if proposals else None
        return {
            "case_id": case.id,
            "title": case.title,
            "category": case.category,
            "status": case.status.value,
            "resolution": case.resolution.value if case.resolution else None,
            "close_reason": case.close_reason,
            "mediator_id": case.mediator_id,
            "party_a_id": case.party_a_id,
            "party_b_id": case.party_b_id,
            "issues": [i.to_dict() for i in self._db.list_issues(case_id)],
            "latest_proposal": latest.to_dict() if latest else None,
            "proposal_count": len(proposals),
            "evidence_summary": summary,
            "minute_count": len(self._db.list_minutes(case_id)),
            "created_at": case.created_at,
            "closed_at": case.closed_at,
        }

    # ------------------------------------------------- 下一步动作与逾期原因
    def _overdue_reasons(self, case: Case, now: datetime) -> list[str]:
        if case.status == CaseStatus.CLOSED:
            return []
        sla = self._sla.get(case.status)
        if sla is None:
            return []
        entered = datetime.fromisoformat(case.status_entered_at)
        elapsed = now - entered
        if elapsed <= sla:
            return []
        days = elapsed.days
        limit = sla.days
        if case.status == CaseStatus.PENDING_ASSIGNMENT:
            return [f"案件待分派已 {days} 天（时限 {limit} 天），尚未分派调解员"]
        if case.status == CaseStatus.ASSIGNED:
            return [f"调解进行中已 {days} 天（时限 {limit} 天），尚未提出方案"]
        if case.status == CaseStatus.PROPOSAL_PENDING:
            pending = self._pending_party_names(case)
            return [f"方案待确认已 {days} 天（时限 {limit} 天），"
                    f"未确认方：{'、'.join(pending)}"]
        if case.status == CaseStatus.AGREED:
            return [f"双方已达成一致 {days} 天（时限 {limit} 天），尚未结案"]
        if case.status == CaseStatus.SUSPENDED:
            return [f"案件已暂缓 {days} 天（时限 {limit} 天），未恢复也未结案"]
        return []

    def _pending_party_names(self, case: Case) -> list[str]:
        proposals = self._db.list_proposals(case.id)
        active = next((p for p in reversed(proposals)
                       if p.status == ProposalStatus.PENDING), None)
        names = []
        for pid in case.party_ids():
            if active is None or active.confirmations.get(pid) != Decision.CONFIRMED:
                party = self._db.get_party(pid)
                names.append(party.name if party else pid)
        return names

    def get_next_actions(self, case_id: str) -> dict:
        """调解员视角：当前应执行的下一步动作与逾期原因。"""
        case = self._require_case(case_id)
        now = self._now()
        actions: list[dict] = []
        if case.status == CaseStatus.PENDING_ASSIGNMENT:
            actions.append({"actor_role": "coordinator",
                            "action": "assign_mediator",
                            "description": "分派调解员"})
        elif case.status == CaseStatus.ASSIGNED:
            actions.append({"actor_role": "mediator",
                            "actor_id": case.mediator_id,
                            "action": "propose_solution",
                            "description": "提出调解方案"})
            actions.append({"actor_role": "mediator",
                            "actor_id": case.mediator_id,
                            "action": "suspend_case",
                            "description": "暂缓调解（如有正当理由）"})
        elif case.status == CaseStatus.PROPOSAL_PENDING:
            for name in self._pending_party_names(case):
                actions.append({"actor_role": "party",
                                "action": "confirm_or_reject_proposal",
                                "description": f"等待 {name} 确认或拒绝方案"})
            actions.append({"actor_role": "mediator",
                            "actor_id": case.mediator_id,
                            "action": "suspend_case",
                            "description": "暂缓调解（如有正当理由）"})
        elif case.status == CaseStatus.AGREED:
            actions.append({"actor_role": "mediator",
                            "actor_id": case.mediator_id,
                            "action": "close_case",
                            "description": "双方已达成一致，办理结案"})
        elif case.status == CaseStatus.SUSPENDED:
            actions.append({"actor_role": "mediator",
                            "actor_id": case.mediator_id,
                            "action": "resume_case",
                            "description": "恢复调解"})
            actions.append({"actor_role": "mediator",
                            "actor_id": case.mediator_id,
                            "action": "close_case",
                            "description": "终止调解并结案"})
        overdue_reasons = self._overdue_reasons(case, now)
        return {
            "case_id": case.id,
            "status": case.status.value,
            "status_entered_at": case.status_entered_at,
            "next_actions": actions,
            "overdue": bool(overdue_reasons),
            "overdue_reasons": overdue_reasons,
        }

    def mediator_dashboard(self, mediator_id: str) -> list[dict]:
        """调解员工作台：名下全部未结案件的下一步动作与逾期原因。"""
        dashboards = []
        for case in self._db.list_cases_by_mediator(mediator_id):
            if case.status == CaseStatus.CLOSED:
                continue
            dashboards.append(self.get_next_actions(case.id))
        return dashboards


class Service(MediationService):
    """领域服务的基础入口（保持原有包入口兼容）。"""


__all__ = ["MediationService", "Service", "DEFAULT_SLA"]
