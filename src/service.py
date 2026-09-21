"""社区矛盾调解协作服务。

将当事人、议题、证据与会谈纪要组织为案件，提供：

- 分派调解员、提出方案、双方确认、暂缓与结案（状态机约束，非法流转直接拒绝）
- 证据去重提交；撤回 / 待核验只是标记，原文与提交时间始终保留
- 隐私字段授权共享：未经当事人授权，另一方无法查看隐私字段
- 会谈纪要乐观锁版本控制，冲突时抛出 :class:`VersionConflictError`
- 案件时间线、下一步动作与逾期原因、结果查询接口

全部状态持久化于 SQLite，服务重启后共享授权与会谈纪要版本保持一致。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .errors import (
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    VersionConflictError,
)
from .models import (
    ALLOWED_TRANSITIONS,
    CaseStatus,
    EvidenceStatus,
    ProposalStatus,
    SlaSettings,
)
from .storage import Database

# ---------------------------------------------------------------------------
# 时间线事件类型
# ---------------------------------------------------------------------------
EVT_CASE_CREATED = "CASE_CREATED"
EVT_PARTY_ADDED = "PARTY_ADDED"
EVT_ISSUE_RAISED = "ISSUE_RAISED"
EVT_MEDIATOR_ASSIGNED = "MEDIATOR_ASSIGNED"
EVT_MEDIATION_STARTED = "MEDIATION_STARTED"
EVT_EVIDENCE_SUBMITTED = "EVIDENCE_SUBMITTED"
EVT_EVIDENCE_VERIFIED = "EVIDENCE_VERIFIED"
EVT_EVIDENCE_WITHDRAWN = "EVIDENCE_WITHDRAWN"
EVT_SESSION_RECORDED = "SESSION_RECORDED"
EVT_MINUTES_UPDATED = "MINUTES_UPDATED"
EVT_PROPOSAL_SUBMITTED = "PROPOSAL_SUBMITTED"
EVT_PROPOSAL_CONFIRMED = "PROPOSAL_CONFIRMED"
EVT_PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
EVT_CASE_RESOLVED = "CASE_RESOLVED"
EVT_CASE_SUSPENDED = "CASE_SUSPENDED"
EVT_CASE_RESUMED = "CASE_RESUMED"
EVT_CASE_CLOSED = "CASE_CLOSED"
EVT_SHARE_GRANTED = "SHARE_GRANTED"
EVT_SHARE_REVOKED = "SHARE_REVOKED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _fmt_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total % 86400 == 0:
        return f"{total // 86400} 天"
    if total % 3600 == 0:
        return f"{total // 3600} 小时"
    return f"{total} 秒"


class MediationService:
    """调解协作领域服务。"""

    def __init__(
        self,
        db_path: str | Path = "mediation.db",
        sla: Optional[SlaSettings] = None,
    ):
        self.db = Database(db_path)
        self.sla = sla or SlaSettings()
        self._lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "MediationService":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[None]:
        """写操作事务：异常时整体回滚，保证不留半成品状态。"""
        with self._lock:
            try:
                yield
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise

    def _require_case(self, case_id: str) -> sqlite3.Row:
        row = self.db.conn.execute(
            "SELECT * FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return row

    def _parties(self, case_id: str) -> list[sqlite3.Row]:
        return self.db.conn.execute(
            "SELECT * FROM parties WHERE case_id = ? ORDER BY created_at, rowid",
            (case_id,),
        ).fetchall()

    def _require_party(self, case_id: str, party_id: str) -> sqlite3.Row:
        row = self.db.conn.execute(
            "SELECT * FROM parties WHERE id = ? AND case_id = ?",
            (party_id, case_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"当事人不存在于该案件：{party_id}")
        return row

    def _require_evidence(self, case_id: str, evidence_id: str) -> sqlite3.Row:
        row = self.db.conn.execute(
            "SELECT * FROM evidence WHERE id = ? AND case_id = ?",
            (evidence_id, case_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"证据不存在于该案件：{evidence_id}")
        return row

    def _require_mediator(self, case: sqlite3.Row, actor: str) -> None:
        if not case["mediator_id"]:
            raise PermissionDeniedError("案件尚未分派调解员")
        if actor != case["mediator_id"]:
            raise PermissionDeniedError("仅案件调解员可执行该操作")

    def _viewer_role(self, case: sqlite3.Row, viewer_id: str) -> str:
        if case["mediator_id"] and viewer_id == case["mediator_id"]:
            return "mediator"
        if any(p["id"] == viewer_id for p in self._parties(case["id"])):
            return "party"
        raise PermissionDeniedError(f"无权查看案件 {case['id']}")

    def _add_event(
        self,
        case_id: str,
        event_type: str,
        actor: str,
        detail: Optional[dict] = None,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO timeline (id, case_id, event_type, actor, detail, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                _new_id("evt"),
                case_id,
                event_type,
                actor,
                json.dumps(detail or {}, ensure_ascii=False),
                _iso(_utcnow()),
            ),
        )

    def _set_status(
        self,
        case: sqlite3.Row,
        target: CaseStatus,
        *,
        previous_status: Optional[str] = None,
        close_reason: Optional[str] = None,
        closed_at: Optional[str] = None,
    ) -> None:
        current = CaseStatus(case["status"])
        if target not in ALLOWED_TRANSITIONS[current]:
            raise InvalidStateTransition(
                f"案件状态不能从 {current.value} 变更为 {target.value}"
            )
        now = _iso(_utcnow())
        self.db.conn.execute(
            """UPDATE cases
               SET status = ?, previous_status = ?,
                   close_reason = COALESCE(?, close_reason),
                   closed_at = COALESCE(?, closed_at),
                   updated_at = ?, status_changed_at = ?
               WHERE id = ?""",
            (target.value, previous_status, close_reason, closed_at, now, now, case["id"]),
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def _case_dict(self, row: sqlite3.Row, parties: Optional[list] = None) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "previous_status": row["previous_status"],
            "mediator_id": row["mediator_id"],
            "close_reason": row["close_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status_changed_at": row["status_changed_at"],
            "closed_at": row["closed_at"],
            "parties": parties if parties is not None else [],
        }

    @staticmethod
    def _party_dict(row: sqlite3.Row, include_contact: bool) -> dict:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "side": row["side"],
            "name": row["name"],
            "contact": row["contact"] if include_contact else None,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _evidence_dict(row: sqlite3.Row, include_private: bool) -> dict:
        private = json.loads(row["private_fields"])
        d = {
            "id": row["id"],
            "case_id": row["case_id"],
            "party_id": row["party_id"],
            "title": row["title"],
            "content": row["content"],
            "content_hash": row["content_hash"],
            "status": row["status"],
            "submitted_at": row["submitted_at"],
            "status_updated_at": row["status_updated_at"],
        }
        if include_private:
            d["private_fields"] = private
            d["private_fields_redacted"] = False
        else:
            d["private_fields"] = None
            d["private_fields_redacted"] = bool(private)
        return d

    @staticmethod
    def _minutes_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "session_no": row["session_no"],
            "content": row["content"],
            "version": row["version"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _grant_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "evidence_id": row["evidence_id"],
            "granted_by": row["granted_by"],
            "granted_to": row["granted_to"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    # ------------------------------------------------------------------
    # 案件与当事人
    # ------------------------------------------------------------------
    def create_case(self, title: str, description: str = "", created_by: str = "system") -> dict:
        """立案。"""
        if not title or not title.strip():
            raise ValidationError("案件标题不能为空")
        case_id = _new_id("case")
        now = _iso(_utcnow())
        with self._tx():
            self.db.conn.execute(
                """INSERT INTO cases
                   (id, title, description, status, previous_status, mediator_id,
                    close_reason, created_at, updated_at, status_changed_at, closed_at)
                   VALUES (?,?,?,?,NULL,NULL,NULL,?,?,?,NULL)""",
                (case_id, title.strip(), description, CaseStatus.INTAKE.value, now, now, now),
            )
            self._add_event(case_id, EVT_CASE_CREATED, created_by, {"title": title.strip()})
        return self.get_case(case_id)

    def get_case(self, case_id: str) -> dict:
        """内部完整视图（含当事人联系方式），供调解员侧与结果查询使用。"""
        case = self._require_case(case_id)
        parties = [self._party_dict(p, include_contact=True) for p in self._parties(case_id)]
        return self._case_dict(case, parties=parties)

    def view_case(self, case_id: str, viewer_id: str) -> dict:
        """按查看者身份返回案件：当事人联系方式属于隐私字段，未授权不对另一方展示。"""
        case = self._require_case(case_id)
        role = self._viewer_role(case, viewer_id)
        parties = []
        for p in self._parties(case_id):
            if role == "mediator" or p["id"] == viewer_id:
                d = self._party_dict(p, include_contact=True)
                d["contact_shared"] = True
            elif self._has_case_level_grant(case_id, p["id"], viewer_id):
                d = self._party_dict(p, include_contact=True)
                d["contact_shared"] = True
            else:
                d = self._party_dict(p, include_contact=False)
                d["contact_shared"] = False
            parties.append(d)
        return self._case_dict(case, parties=parties)

    def add_party(self, case_id: str, name: str, contact: str = "") -> dict:
        """登记当事人（每案双方：先登记为申请方 A，后为被申请方 B）。"""
        case = self._require_case(case_id)
        if CaseStatus(case["status"]) not in (CaseStatus.INTAKE, CaseStatus.ASSIGNED):
            raise InvalidStateTransition("调解开始后不能再新增当事人")
        if not name or not name.strip():
            raise ValidationError("当事人姓名不能为空")
        with self._tx():
            parties = self._parties(case_id)
            if len(parties) >= 2:
                raise ValidationError("一个案件最多登记双方当事人")
            side = "A" if not parties else "B"
            party_id = _new_id("party")
            self.db.conn.execute(
                "INSERT INTO parties (id, case_id, side, name, contact, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (party_id, case_id, side, name.strip(), contact, _iso(_utcnow())),
            )
            self._add_event(
                case_id, EVT_PARTY_ADDED, party_id, {"name": name.strip(), "side": side}
            )
        return self._party_dict(self._require_party(case_id, party_id), include_contact=True)

    def add_issue(
        self, case_id: str, title: str, detail: str = "", raised_by: Optional[str] = None
    ) -> dict:
        """登记争议议题。"""
        case = self._require_case(case_id)
        if CaseStatus(case["status"]) in (CaseStatus.RESOLVED, CaseStatus.CLOSED):
            raise InvalidStateTransition("当前状态不能新增议题")
        if not title or not title.strip():
            raise ValidationError("议题标题不能为空")
        if raised_by is not None:
            self._require_party(case_id, raised_by)
        issue_id = _new_id("issue")
        with self._tx():
            self.db.conn.execute(
                "INSERT INTO issues (id, case_id, title, detail, raised_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (issue_id, case_id, title.strip(), detail, raised_by, _iso(_utcnow())),
            )
            self._add_event(
                case_id, EVT_ISSUE_RAISED, raised_by or "system",
                {"issue_id": issue_id, "title": title.strip()},
            )
        return {
            "id": issue_id,
            "case_id": case_id,
            "title": title.strip(),
            "detail": detail,
            "raised_by": raised_by,
        }

    def list_issues(self, case_id: str) -> list[dict]:
        self._require_case(case_id)
        rows = self.db.conn.execute(
            "SELECT * FROM issues WHERE case_id = ? ORDER BY created_at, rowid", (case_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 状态机：分派 / 开始 / 暂缓 / 恢复 / 结案
    # ------------------------------------------------------------------
    def assign_mediator(self, case_id: str, mediator_id: str, actor: str = "system") -> dict:
        """分派调解员：INTAKE -> ASSIGNED。"""
        case = self._require_case(case_id)
        if not mediator_id or not mediator_id.strip():
            raise ValidationError("调解员标识不能为空")
        with self._tx():
            if len(self._parties(case_id)) < 2:
                raise ValidationError("需先登记双方当事人，再分派调解员")
            self._set_status(case, CaseStatus.ASSIGNED)
            self.db.conn.execute(
                "UPDATE cases SET mediator_id = ?, updated_at = ? WHERE id = ?",
                (mediator_id.strip(), _iso(_utcnow()), case_id),
            )
            self._add_event(
                case_id, EVT_MEDIATOR_ASSIGNED, actor, {"mediator_id": mediator_id.strip()}
            )
        return self.get_case(case_id)

    def start_mediation(self, case_id: str, actor: str) -> dict:
        """开始调解：ASSIGNED -> IN_MEDIATION。"""
        case = self._require_case(case_id)
        self._require_mediator(case, actor)
        with self._tx():
            self._set_status(case, CaseStatus.IN_MEDIATION)
            self._add_event(case_id, EVT_MEDIATION_STARTED, actor)
        return self.get_case(case_id)

    def suspend_case(self, case_id: str, reason: str, actor: str) -> dict:
        """暂缓调解：ASSIGNED / IN_MEDIATION / PROPOSAL_REVIEW -> SUSPENDED。"""
        case = self._require_case(case_id)
        self._require_mediator(case, actor)
        if not reason or not reason.strip():
            raise ValidationError("暂缓必须说明原因")
        current = CaseStatus(case["status"])
        with self._tx():
            self._set_status(case, CaseStatus.SUSPENDED, previous_status=current.value)
            self._add_event(
                case_id, EVT_CASE_SUSPENDED, actor,
                {"reason": reason.strip(), "previous_status": current.value},
            )
        return self.get_case(case_id)

    def resume_case(self, case_id: str, actor: str) -> dict:
        """恢复调解：SUSPENDED -> 暂缓前状态。"""
        case = self._require_case(case_id)
        self._require_mediator(case, actor)
        if CaseStatus(case["status"]) is not CaseStatus.SUSPENDED:
            raise InvalidStateTransition("仅暂缓中的案件可以恢复")
        if not case["previous_status"]:
            raise InvalidStateTransition("缺少暂缓前状态，无法恢复")
        target = CaseStatus(case["previous_status"])
        with self._tx():
            self._set_status(case, target, previous_status=None)
            self._add_event(case_id, EVT_CASE_RESUMED, actor, {"resumed_to": target.value})
        return self.get_case(case_id)

    def close_case(self, case_id: str, actor: str, reason: str = "") -> dict:
        """结案：RESOLVED / IN_MEDIATION / SUSPENDED -> CLOSED。

        双方已确认方案（RESOLVED）时原因可选；其他情形必须说明结案原因。
        """
        case = self._require_case(case_id)
        self._require_mediator(case, actor)
        current = CaseStatus(case["status"])
        reason = reason.strip()
        if current is CaseStatus.RESOLVED and not reason:
            reason = "双方已确认调解方案"
        if current is not CaseStatus.RESOLVED and not reason:
            raise ValidationError("未达成协议的结案必须说明原因")
        with self._tx():
            self._set_status(
                case, CaseStatus.CLOSED, close_reason=reason, closed_at=_iso(_utcnow())
            )
            self._add_event(case_id, EVT_CASE_CLOSED, actor, {"reason": reason})
        return self.get_case(case_id)

    # ------------------------------------------------------------------
    # 证据
    # ------------------------------------------------------------------
    def submit_evidence(
        self,
        case_id: str,
        party_id: str,
        title: str,
        content: str,
        private_fields: Optional[dict] = None,
        submitted_at: Optional[datetime] = None,
    ) -> dict:
        """提交证据。

        同一当事人在同一案件下重复提交相同内容不会产生副本，
        直接返回已存在的证据（``deduplicated=True``），也不重复写时间线。
        """
        case = self._require_case(case_id)
        if CaseStatus(case["status"]) is CaseStatus.CLOSED:
            raise InvalidStateTransition("案件已结案，不能提交证据")
        self._require_party(case_id, party_id)
        if not content or not content.strip():
            raise ValidationError("证据内容不能为空")
        if private_fields is not None and not isinstance(private_fields, dict):
            raise ValidationError("private_fields 必须是字典")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._tx():
            existing = self.db.conn.execute(
                "SELECT * FROM evidence WHERE case_id = ? AND party_id = ? AND content_hash = ?",
                (case_id, party_id, digest),
            ).fetchone()
            if existing is not None:
                out = self._evidence_dict(existing, include_private=True)
                out["deduplicated"] = True
                return out
            evidence_id = _new_id("ev")
            now = _iso(_utcnow())
            self.db.conn.execute(
                """INSERT INTO evidence
                   (id, case_id, party_id, title, content, content_hash,
                    private_fields, status, submitted_at, status_updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence_id,
                    case_id,
                    party_id,
                    (title or "").strip(),
                    content,
                    digest,
                    json.dumps(private_fields or {}, ensure_ascii=False),
                    EvidenceStatus.PENDING_VERIFICATION.value,
                    _iso(submitted_at) if submitted_at else now,
                    now,
                ),
            )
            # 时间线只记录标题与哈希，避免泄露隐私字段
            self._add_event(
                case_id, EVT_EVIDENCE_SUBMITTED, party_id,
                {"evidence_id": evidence_id, "title": (title or "").strip(),
                 "content_hash": digest},
            )
            row = self._require_evidence(case_id, evidence_id)
            out = self._evidence_dict(row, include_private=True)
            out["deduplicated"] = False
            return out

    def verify_evidence(self, case_id: str, evidence_id: str, actor: str) -> dict:
        """调解员核验证据：PENDING_VERIFICATION -> VERIFIED。"""
        case = self._require_case(case_id)
        self._require_mediator(case, actor)
        ev = self._require_evidence(case_id, evidence_id)
        if ev["status"] != EvidenceStatus.PENDING_VERIFICATION.value:
            raise ValidationError("仅待核验证据可标记为已核验")
        with self._tx():
            self.db.conn.execute(
                "UPDATE evidence SET status = ?, status_updated_at = ? WHERE id = ?",
                (EvidenceStatus.VERIFIED.value, _iso(_utcnow()), evidence_id),
            )
            self._add_event(case_id, EVT_EVIDENCE_VERIFIED, actor, {"evidence_id": evidence_id})
        return self._evidence_dict(self._require_evidence(case_id, evidence_id), True)

    def withdraw_evidence(self, case_id: str, evidence_id: str, actor: str) -> dict:
        """提交方撤回证据：标记 WITHDRAWN，原文与提交时间保留。"""
        self._require_case(case_id)
        ev = self._require_evidence(case_id, evidence_id)
        if ev["party_id"] != actor:
            raise PermissionDeniedError("仅证据提交方可撤回该证据")
        if ev["status"] == EvidenceStatus.WITHDRAWN.value:
            raise ValidationError("证据已撤回")
        with self._tx():
            self.db.conn.execute(
                "UPDATE evidence SET status = ?, status_updated_at = ? WHERE id = ?",
                (EvidenceStatus.WITHDRAWN.value, _iso(_utcnow()), evidence_id),
            )
            self._add_event(
                case_id, EVT_EVIDENCE_WITHDRAWN, actor, {"evidence_id": evidence_id}
            )
        return self._evidence_dict(self._require_evidence(case_id, evidence_id), True)

    def get_evidence(self, case_id: str, evidence_id: str) -> dict:
        """内部完整视图（含隐私字段）。"""
        self._require_case(case_id)
        return self._evidence_dict(self._require_evidence(case_id, evidence_id), True)

    def list_evidence(self, case_id: str) -> list[dict]:
        """内部完整列表（含隐私字段），供调解员侧使用。"""
        self._require_case(case_id)
        rows = self.db.conn.execute(
            "SELECT * FROM evidence WHERE case_id = ? ORDER BY submitted_at, rowid",
            (case_id,),
        ).fetchall()
        return [self._evidence_dict(r, include_private=True) for r in rows]

    def view_evidence(self, case_id: str, viewer_id: str) -> list[dict]:
        """按查看者身份返回证据列表。

        隐私字段仅调解员与提交方可见；另一方只有在持有有效授权
        （案件级或该条证据级）时才能看到，否则被遮蔽。
        """
        case = self._require_case(case_id)
        role = self._viewer_role(case, viewer_id)
        rows = self.db.conn.execute(
            "SELECT * FROM evidence WHERE case_id = ? ORDER BY submitted_at, rowid",
            (case_id,),
        ).fetchall()
        result = []
        for row in rows:
            include = (
                role == "mediator"
                or row["party_id"] == viewer_id
                or self._has_evidence_access(case_id, row["id"], row["party_id"], viewer_id)
            )
            result.append(self._evidence_dict(row, include_private=include))
        return result

    # ------------------------------------------------------------------
    # 授权共享
    # ------------------------------------------------------------------
    def _has_case_level_grant(self, case_id: str, granted_by: str, granted_to: str) -> bool:
        row = self.db.conn.execute(
            """SELECT 1 FROM share_grants
               WHERE case_id = ? AND granted_by = ? AND granted_to = ?
                 AND active = 1 AND evidence_id IS NULL LIMIT 1""",
            (case_id, granted_by, granted_to),
        ).fetchone()
        return row is not None

    def _has_evidence_access(
        self, case_id: str, evidence_id: str, owner_party_id: str, viewer_party_id: str
    ) -> bool:
        row = self.db.conn.execute(
            """SELECT 1 FROM share_grants
               WHERE case_id = ? AND granted_by = ? AND granted_to = ? AND active = 1
                 AND (evidence_id IS NULL OR evidence_id = ?) LIMIT 1""",
            (case_id, owner_party_id, viewer_party_id, evidence_id),
        ).fetchone()
        return row is not None

    def grant_share(
        self, case_id: str, granted_by: str, evidence_id: Optional[str] = None
    ) -> dict:
        """当事人授权另一方查看本方隐私字段。

        ``evidence_id=None`` 表示案件级授权（覆盖本方全部证据的隐私字段与联系方式）；
        否则仅授权该条证据（证据须为本方提交）。重复授权幂等，不产生重复授权记录。
        """
        case = self._require_case(case_id)
        if CaseStatus(case["status"]) is CaseStatus.CLOSED:
            raise InvalidStateTransition("案件已结案，不能再授权共享")
        self._require_party(case_id, granted_by)
        others = [p for p in self._parties(case_id) if p["id"] != granted_by]
        if not others:
            raise ValidationError("案件尚无可共享的另一方当事人")
        granted_to = others[0]["id"]
        if evidence_id is not None:
            ev = self._require_evidence(case_id, evidence_id)
            if ev["party_id"] != granted_by:
                raise PermissionDeniedError("只能授权共享本方提交的证据")
        with self._tx():
            existing = self.db.conn.execute(
                """SELECT * FROM share_grants
                   WHERE case_id = ? AND granted_by = ? AND granted_to = ?
                     AND active = 1 AND evidence_id IS ?""",
                (case_id, granted_by, granted_to, evidence_id),
            ).fetchone()
            if existing is not None:
                return self._grant_dict(existing)
            grant_id = _new_id("grant")
            self.db.conn.execute(
                """INSERT INTO share_grants
                   (id, case_id, evidence_id, granted_by, granted_to, active, created_at, revoked_at)
                   VALUES (?,?,?,?,?,1,?,NULL)""",
                (grant_id, case_id, evidence_id, granted_by, granted_to, _iso(_utcnow())),
            )
            self._add_event(
                case_id, EVT_SHARE_GRANTED, granted_by,
                {"grant_id": grant_id, "granted_to": granted_to,
                 "evidence_id": evidence_id,
                 "scope": "evidence" if evidence_id else "case"},
            )
        return self.get_share_grant(case_id, grant_id)

    def revoke_share(self, case_id: str, grant_id: str, actor: str) -> dict:
        """撤销共享授权：授权方当事人或调解员可撤销。"""
        case = self._require_case(case_id)
        grant = self.db.conn.execute(
            "SELECT * FROM share_grants WHERE id = ? AND case_id = ?",
            (grant_id, case_id),
        ).fetchone()
        if grant is None:
            raise NotFoundError(f"授权记录不存在于该案件：{grant_id}")
        if actor != grant["granted_by"] and actor != case["mediator_id"]:
            raise PermissionDeniedError("仅授权方当事人或调解员可撤销共享授权")
        with self._tx():
            if grant["active"]:
                self.db.conn.execute(
                    "UPDATE share_grants SET active = 0, revoked_at = ? WHERE id = ?",
                    (_iso(_utcnow()), grant_id),
                )
                self._add_event(case_id, EVT_SHARE_REVOKED, actor, {"grant_id": grant_id})
        return self.get_share_grant(case_id, grant_id)

    def get_share_grant(self, case_id: str, grant_id: str) -> dict:
        self._require_case(case_id)
        row = self.db.conn.execute(
            "SELECT * FROM share_grants WHERE id = ? AND case_id = ?",
            (grant_id, case_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"授权记录不存在于该案件：{grant_id}")
        return self._grant_dict(row)

    def list_share_grants(self, case_id: str, include_revoked: bool = False) -> list[dict]:
        self._require_case(case_id)
        sql = "SELECT * FROM share_grants WHERE case_id = ?"
        if not include_revoked:
            sql += " AND active = 1"
        sql += " ORDER BY created_at, rowid"
        rows = self.db.conn.execute(sql, (case_id,)).fetchall()
        return [self._grant_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 会谈纪要（乐观锁版本控制）
    # ------------------------------------------------------------------
    def record_minutes(self, case_id: str, session_no: int, content: str, author: str) -> dict:
        """记录会谈纪要（初始版本 v1）。仅调解中可记录。"""
        case = self._require_case(case_id)
        self._require_mediator(case, author)
        if CaseStatus(case["status"]) is not CaseStatus.IN_MEDIATION:
            raise InvalidStateTransition("仅调解中可记录会谈纪要")
        if not content or not content.strip():
            raise ValidationError("纪要内容不能为空")
        minutes_id = _new_id("min")
        now = _iso(_utcnow())
        with self._tx():
            self.db.conn.execute(
                """INSERT INTO minutes
                   (id, case_id, session_no, content, version, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,1,?,?,?)""",
                (minutes_id, case_id, session_no, content, author, now, now),
            )
            self.db.conn.execute(
                """INSERT INTO minute_versions
                   (id, minutes_id, version, content, edited_by, edited_at)
                   VALUES (?,?,1,?,?,?)""",
                (_new_id("mv"), minutes_id, content, author, now),
            )
            self._add_event(
                case_id, EVT_SESSION_RECORDED, author,
                {"minutes_id": minutes_id, "session_no": session_no},
            )
        return self.get_minute(case_id, minutes_id)

    def update_minutes(
        self,
        case_id: str,
        minutes_id: str,
        expected_version: int,
        content: str,
        editor: str,
    ) -> dict:
        """编辑纪要：``expected_version`` 与当前版本不一致时抛出
        :class:`VersionConflictError`，本次写入整体放弃。"""
        case = self._require_case(case_id)
        self._require_mediator(case, editor)
        if CaseStatus(case["status"]) is CaseStatus.CLOSED:
            raise InvalidStateTransition("案件已结案，纪要不可再修改")
        if not content or not content.strip():
            raise ValidationError("纪要内容不能为空")
        with self._tx():
            row = self.db.conn.execute(
                "SELECT * FROM minutes WHERE id = ? AND case_id = ?",
                (minutes_id, case_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"会谈纪要不存在于该案件：{minutes_id}")
            current = row["version"]
            if current != expected_version:
                raise VersionConflictError(minutes_id, expected_version, current)
            new_version = current + 1
            now = _iso(_utcnow())
            self.db.conn.execute(
                "UPDATE minutes SET content = ?, version = ?, updated_at = ? WHERE id = ?",
                (content, new_version, now, minutes_id),
            )
            self.db.conn.execute(
                """INSERT INTO minute_versions
                   (id, minutes_id, version, content, edited_by, edited_at)
                   VALUES (?,?,?,?,?,?)""",
                (_new_id("mv"), minutes_id, new_version, content, editor, now),
            )
            self._add_event(
                case_id, EVT_MINUTES_UPDATED, editor,
                {"minutes_id": minutes_id, "version": new_version},
            )
        return self.get_minute(case_id, minutes_id)

    def get_minute(self, case_id: str, minutes_id: str) -> dict:
        self._require_case(case_id)
        row = self.db.conn.execute(
            "SELECT * FROM minutes WHERE id = ? AND case_id = ?",
            (minutes_id, case_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"会谈纪要不存在于该案件：{minutes_id}")
        return self._minutes_dict(row)

    def get_minutes(self, case_id: str) -> list[dict]:
        self._require_case(case_id)
        rows = self.db.conn.execute(
            "SELECT * FROM minutes WHERE case_id = ? ORDER BY session_no, created_at",
            (case_id,),
        ).fetchall()
        return [self._minutes_dict(r) for r in rows]

    def get_minutes_history(self, case_id: str, minutes_id: str) -> list[dict]:
        """纪要全部历史版本（含每一次编辑的内容与操作人）。"""
        self.get_minute(case_id, minutes_id)
        rows = self.db.conn.execute(
            "SELECT * FROM minute_versions WHERE minutes_id = ? ORDER BY version",
            (minutes_id,),
        ).fetchall()
        return [
            {
                "version": r["version"],
                "content": r["content"],
                "edited_by": r["edited_by"],
                "edited_at": r["edited_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 调解方案与双方确认
    # ------------------------------------------------------------------
    def propose_settlement(self, case_id: str, content: str, actor: str) -> dict:
        """提出调解方案：IN_MEDIATION -> PROPOSAL_REVIEW。"""
        case = self._require_case(case_id)
        self._require_mediator(case, actor)
        if not content or not content.strip():
            raise ValidationError("方案内容不能为空")
        with self._tx():
            pending = self.db.conn.execute(
                "SELECT 1 FROM proposals WHERE case_id = ? AND status = ?",
                (case_id, ProposalStatus.PENDING.value),
            ).fetchone()
            if pending is not None:
                raise ValidationError("已有待确认的方案，不能重复提出")
            proposal_id = _new_id("prop")
            self.db.conn.execute(
                """INSERT INTO proposals
                   (id, case_id, content, proposed_by, status, created_at, resolved_at)
                   VALUES (?,?,?,?,?,?,NULL)""",
                (proposal_id, case_id, content, actor,
                 ProposalStatus.PENDING.value, _iso(_utcnow())),
            )
            self._set_status(case, CaseStatus.PROPOSAL_REVIEW)
            self._add_event(
                case_id, EVT_PROPOSAL_SUBMITTED, actor, {"proposal_id": proposal_id}
            )
        return self.get_proposal(case_id, proposal_id)

    def confirm_proposal(
        self,
        case_id: str,
        proposal_id: str,
        party_id: str,
        accept: bool = True,
        note: str = "",
    ) -> dict:
        """当事人对方案表态。

        任一方拒绝：方案 REJECTED，案件回到 IN_MEDIATION；
        双方均接受：方案 CONFIRMED，案件进入 RESOLVED。
        """
        case = self._require_case(case_id)
        if CaseStatus(case["status"]) is not CaseStatus.PROPOSAL_REVIEW:
            raise InvalidStateTransition("当前状态不在方案确认阶段")
        proposal = self.db.conn.execute(
            "SELECT * FROM proposals WHERE id = ? AND case_id = ?",
            (proposal_id, case_id),
        ).fetchone()
        if proposal is None:
            raise NotFoundError(f"方案不存在于该案件：{proposal_id}")
        if proposal["status"] != ProposalStatus.PENDING.value:
            raise ValidationError("该方案已处理完毕")
        self._require_party(case_id, party_id)
        with self._tx():
            dup = self.db.conn.execute(
                "SELECT 1 FROM proposal_confirmations WHERE proposal_id = ? AND party_id = ?",
                (proposal_id, party_id),
            ).fetchone()
            if dup is not None:
                raise ValidationError("该方已对本方案表态，不能重复操作")
            now = _iso(_utcnow())
            self.db.conn.execute(
                """INSERT INTO proposal_confirmations
                   (id, proposal_id, party_id, accept, note, decided_at)
                   VALUES (?,?,?,?,?,?)""",
                (_new_id("conf"), proposal_id, party_id, 1 if accept else 0, note, now),
            )
            if not accept:
                self.db.conn.execute(
                    "UPDATE proposals SET status = ?, resolved_at = ? WHERE id = ?",
                    (ProposalStatus.REJECTED.value, now, proposal_id),
                )
                self._set_status(case, CaseStatus.IN_MEDIATION)
                self._add_event(
                    case_id, EVT_PROPOSAL_REJECTED, party_id,
                    {"proposal_id": proposal_id, "note": note},
                )
            else:
                self._add_event(
                    case_id, EVT_PROPOSAL_CONFIRMED, party_id,
                    {"proposal_id": proposal_id},
                )
                parties = self._parties(case_id)
                decided = {
                    r["party_id"]
                    for r in self.db.conn.execute(
                        "SELECT party_id FROM proposal_confirmations WHERE proposal_id = ? AND accept = 1",
                        (proposal_id,),
                    ).fetchall()
                }
                if all(p["id"] in decided for p in parties):
                    self.db.conn.execute(
                        "UPDATE proposals SET status = ?, resolved_at = ? WHERE id = ?",
                        (ProposalStatus.CONFIRMED.value, now, proposal_id),
                    )
                    self._set_status(case, CaseStatus.RESOLVED)
                    self._add_event(
                        case_id, EVT_CASE_RESOLVED, "system", {"proposal_id": proposal_id}
                    )
        return self.get_proposal(case_id, proposal_id)

    def get_proposal(self, case_id: str, proposal_id: Optional[str] = None) -> dict:
        """查询方案（默认最新一条）及双方表态。"""
        self._require_case(case_id)
        if proposal_id is None:
            row = self.db.conn.execute(
                "SELECT * FROM proposals WHERE case_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        else:
            row = self.db.conn.execute(
                "SELECT * FROM proposals WHERE id = ? AND case_id = ?",
                (proposal_id, case_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("该案件暂无调解方案" if proposal_id is None else f"方案不存在：{proposal_id}")
        confirmations = self.db.conn.execute(
            """SELECT c.*, p.name AS party_name
               FROM proposal_confirmations c
               JOIN parties p ON p.id = c.party_id
               WHERE c.proposal_id = ? ORDER BY c.decided_at, c.rowid""",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "content": row["content"],
            "proposed_by": row["proposed_by"],
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "confirmations": [
                {
                    "party_id": c["party_id"],
                    "party_name": c["party_name"],
                    "accept": bool(c["accept"]),
                    "note": c["note"],
                    "decided_at": c["decided_at"],
                }
                for c in confirmations
            ],
        }

    # ------------------------------------------------------------------
    # 时间线 / 工作台 / 结果查询
    # ------------------------------------------------------------------
    def get_timeline(self, case_id: str) -> list[dict]:
        """案件时间线：全部留痕事件按发生顺序返回。"""
        self._require_case(case_id)
        rows = self.db.conn.execute(
            "SELECT * FROM timeline WHERE case_id = ? ORDER BY rowid", (case_id,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "actor": r["actor"],
                "detail": json.loads(r["detail"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_next_actions(self, case_id: str) -> list[dict]:
        """调解员工作台：当前状态下的下一步动作建议。"""
        case = self._require_case(case_id)
        status = CaseStatus(case["status"])
        actions: list[dict] = []
        if status is CaseStatus.INTAKE:
            if len(self._parties(case_id)) < 2:
                actions.append({"action": "add_party", "detail": "登记双方当事人"})
            else:
                actions.append({"action": "assign_mediator", "detail": "分派调解员"})
        elif status is CaseStatus.ASSIGNED:
            actions.append({"action": "start_mediation", "detail": "开始调解并记录首次会谈纪要"})
        elif status is CaseStatus.IN_MEDIATION:
            pending = self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM evidence WHERE case_id = ? AND status = ?",
                (case_id, EvidenceStatus.PENDING_VERIFICATION.value),
            ).fetchone()["n"]
            if pending:
                actions.append(
                    {"action": "verify_evidence", "detail": f"核验 {pending} 份待核验证据"}
                )
            actions.append({"action": "record_minutes", "detail": "记录会谈纪要"})
            actions.append({"action": "propose_settlement", "detail": "提出调解方案"})
            actions.append({"action": "suspend_case", "detail": "暂缓调解（需说明原因）"})
        elif status is CaseStatus.PROPOSAL_REVIEW:
            awaiting = self._awaiting_parties(case_id)
            names = "、".join(p["name"] for p in awaiting) or "—"
            actions.append(
                {"action": "await_confirmation", "detail": f"等待双方确认方案（未表态：{names}）"}
            )
        elif status is CaseStatus.RESOLVED:
            actions.append({"action": "close_case", "detail": "双方已确认方案，办理结案"})
        elif status is CaseStatus.SUSPENDED:
            actions.append({"action": "resume_case", "detail": "恢复调解"})
            actions.append({"action": "close_case", "detail": "终止调解并结案（需说明原因）"})
        return actions

    def _awaiting_parties(self, case_id: str) -> list[sqlite3.Row]:
        proposal = self.db.conn.execute(
            "SELECT id FROM proposals WHERE case_id = ? AND status = ?",
            (case_id, ProposalStatus.PENDING.value),
        ).fetchone()
        if proposal is None:
            return []
        return [
            p
            for p in self._parties(case_id)
            if self.db.conn.execute(
                "SELECT 1 FROM proposal_confirmations WHERE proposal_id = ? AND party_id = ?",
                (proposal["id"], p["id"]),
            ).fetchone()
            is None
        ]

    def get_overdue_reasons(self, case_id: str, now: Optional[datetime] = None) -> list[str]:
        """调解员工作台：当前逾期原因（无逾期返回空列表）。"""
        case = self._require_case(case_id)
        now = now or _utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        status = CaseStatus(case["status"])
        reasons: list[str] = []
        changed = _parse(case["status_changed_at"])
        created = _parse(case["created_at"])
        sla = self.sla
        if status is CaseStatus.INTAKE and now - created > sla.assign:
            reasons.append(f"立案已超过 {_fmt_duration(sla.assign)} 未分派调解员")
        if status is CaseStatus.ASSIGNED and now - changed > sla.first_session:
            reasons.append(f"分派调解员已超过 {_fmt_duration(sla.first_session)} 未开始调解")
        if status is CaseStatus.PROPOSAL_REVIEW:
            proposal = self.db.conn.execute(
                "SELECT * FROM proposals WHERE case_id = ? AND status = ?",
                (case_id, ProposalStatus.PENDING.value),
            ).fetchone()
            if proposal is not None and now - _parse(proposal["created_at"]) > sla.proposal_confirm:
                awaiting = "、".join(p["name"] for p in self._awaiting_parties(case_id)) or "—"
                reasons.append(
                    f"调解方案提交已超过 {_fmt_duration(sla.proposal_confirm)} "
                    f"仍未获双方确认（待表态：{awaiting}）"
                )
        if status is CaseStatus.SUSPENDED and now - changed > sla.suspension:
            reasons.append(f"案件暂缓已超过 {_fmt_duration(sla.suspension)} 未恢复")
        stale = self.db.conn.execute(
            "SELECT submitted_at FROM evidence WHERE case_id = ? AND status = ?",
            (case_id, EvidenceStatus.PENDING_VERIFICATION.value),
        ).fetchall()
        n_stale = sum(1 for r in stale if now - _parse(r["submitted_at"]) > sla.evidence_verify)
        if n_stale:
            reasons.append(f"{n_stale} 份证据提交超过 {_fmt_duration(sla.evidence_verify)} 仍待核验")
        return reasons

    def get_case_result(self, case_id: str) -> dict:
        """结果查询：案件结论、达成的方案与确认记录、证据与议题汇总。"""
        case = self._require_case(case_id)
        parties = self._parties(case_id)
        agreement = None
        proposal = self.db.conn.execute(
            "SELECT * FROM proposals WHERE case_id = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
            (case_id, ProposalStatus.CONFIRMED.value),
        ).fetchone()
        if proposal is not None:
            full = self.get_proposal(case_id, proposal["id"])
            agreement = {
                "proposal_id": full["id"],
                "content": full["content"],
                "confirmed_at": full["resolved_at"],
                "confirmations": full["confirmations"],
            }
        ev_rows = self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM evidence WHERE case_id = ? GROUP BY status",
            (case_id,),
        ).fetchall()
        ev_counts = {r["status"]: r["n"] for r in ev_rows}
        return {
            "case_id": case["id"],
            "title": case["title"],
            "status": case["status"],
            "mediator_id": case["mediator_id"],
            "parties": [
                {"id": p["id"], "side": p["side"], "name": p["name"]} for p in parties
            ],
            "issues": self.list_issues(case_id),
            "agreement": agreement,
            "close_reason": case["close_reason"],
            "closed_at": case["closed_at"],
            "suspended_from": case["previous_status"]
            if case["status"] == CaseStatus.SUSPENDED.value
            else None,
            "evidence_summary": {
                "total": sum(ev_counts.values()),
                "verified": ev_counts.get(EvidenceStatus.VERIFIED.value, 0),
                "pending_verification": ev_counts.get(
                    EvidenceStatus.PENDING_VERIFICATION.value, 0
                ),
                "withdrawn": ev_counts.get(EvidenceStatus.WITHDRAWN.value, 0),
            },
        }


class Service(MediationService):
    """向后兼容的服务入口（默认使用 mediation.db）。"""

    def __init__(self, db_path: str | Path = "mediation.db", sla: Optional[SlaSettings] = None):
        super().__init__(db_path, sla)
        self.ready = True
