"""SQLite 持久化层。

所有写操作即时落库（写穿），服务重启后用同一数据库文件重新实例化
MediationService 即可恢复全部状态——包括共享授权与会谈纪要版本。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from .models import (
    Case,
    CaseStatus,
    Decision,
    Evidence,
    EvidenceStatus,
    EventType,
    GrantStatus,
    Issue,
    IssueStatus,
    Minute,
    MinuteVersion,
    Party,
    Proposal,
    ProposalStatus,
    Resolution,
    ShareGrant,
    TimelineEvent,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    party_a_id TEXT NOT NULL,
    party_b_id TEXT NOT NULL,
    mediator_id TEXT,
    status TEXT NOT NULL,
    status_before_suspend TEXT,
    resolution TEXT,
    close_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status_entered_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    content TEXT NOT NULL,
    private_fields TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    status_updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minutes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minute_versions (
    minute_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    edited_by TEXT NOT NULL,
    edited_at TEXT NOT NULL,
    PRIMARY KEY (minute_id, version)
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    content TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS confirmations (
    proposal_id TEXT NOT NULL,
    party_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (proposal_id, party_id)
);

CREATE TABLE IF NOT EXISTS share_grants (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    evidence_id TEXT,
    granted_by TEXT NOT NULL,
    granted_to TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id);
CREATE INDEX IF NOT EXISTS idx_cases_mediator ON cases(mediator_id);
"""


def _loads(text: str) -> dict:
    return json.loads(text) if text else {}


class Database:
    """对 sqlite3 的薄封装：建表、行映射、线程安全。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ utils
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- parties
    def insert_party(self, party: Party) -> None:
        self._execute(
            "INSERT INTO parties (id, name, contact) VALUES (?, ?, ?)",
            (party.id, party.name, party.contact),
        )

    def get_party(self, party_id: str) -> Optional[Party]:
        row = self._query_one("SELECT * FROM parties WHERE id = ?", (party_id,))
        return Party(**dict(row)) if row else None

    # ------------------------------------------------------------------ cases
    def insert_case(self, case: Case) -> None:
        self._execute(
            """INSERT INTO cases (id, title, description, category, party_a_id,
                   party_b_id, mediator_id, status, status_before_suspend,
                   resolution, close_reason, created_at, updated_at,
                   status_entered_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case.id, case.title, case.description, case.category,
                case.party_a_id, case.party_b_id, case.mediator_id,
                case.status.value,
                case.status_before_suspend.value if case.status_before_suspend else None,
                case.resolution.value if case.resolution else None,
                case.close_reason, case.created_at, case.updated_at,
                case.status_entered_at, case.closed_at,
            ),
        )

    def update_case(self, case: Case) -> None:
        self._execute(
            """UPDATE cases SET mediator_id = ?, status = ?, status_before_suspend = ?,
                   resolution = ?, close_reason = ?, updated_at = ?,
                   status_entered_at = ?, closed_at = ?
               WHERE id = ?""",
            (
                case.mediator_id, case.status.value,
                case.status_before_suspend.value if case.status_before_suspend else None,
                case.resolution.value if case.resolution else None,
                case.close_reason, case.updated_at, case.status_entered_at,
                case.closed_at, case.id,
            ),
        )

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> Case:
        data = dict(row)
        data["status"] = CaseStatus(data["status"])
        if data["status_before_suspend"]:
            data["status_before_suspend"] = CaseStatus(data["status_before_suspend"])
        if data["resolution"]:
            data["resolution"] = Resolution(data["resolution"])
        return Case(**data)

    def get_case(self, case_id: str) -> Optional[Case]:
        row = self._query_one("SELECT * FROM cases WHERE id = ?", (case_id,))
        return self._row_to_case(row) if row else None

    def list_cases_by_mediator(self, mediator_id: str) -> list[Case]:
        rows = self._query_all(
            "SELECT * FROM cases WHERE mediator_id = ? ORDER BY created_at",
            (mediator_id,),
        )
        return [self._row_to_case(r) for r in rows]

    # ----------------------------------------------------------------- issues
    def insert_issue(self, issue: Issue) -> None:
        self._execute(
            """INSERT INTO issues (id, case_id, title, description, raised_by,
                   status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (issue.id, issue.case_id, issue.title, issue.description,
             issue.raised_by, issue.status.value, issue.created_at),
        )

    def list_issues(self, case_id: str) -> list[Issue]:
        rows = self._query_all(
            "SELECT * FROM issues WHERE case_id = ? ORDER BY created_at", (case_id,)
        )
        result = []
        for r in rows:
            data = dict(r)
            data["status"] = IssueStatus(data["status"])
            result.append(Issue(**data))
        return result

    # --------------------------------------------------------------- evidence
    def insert_evidence(self, ev: Evidence) -> None:
        self._execute(
            """INSERT INTO evidence (id, case_id, submitted_by, content,
                   private_fields, content_hash, status, submitted_at,
                   status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev.id, ev.case_id, ev.submitted_by, ev.content,
             json.dumps(ev.private_fields, ensure_ascii=False, sort_keys=True),
             ev.content_hash, ev.status.value, ev.submitted_at, ev.status_updated_at),
        )

    def update_evidence_status(self, evidence_id: str, status: EvidenceStatus,
                               status_updated_at: str) -> None:
        self._execute(
            "UPDATE evidence SET status = ?, status_updated_at = ? WHERE id = ?",
            (status.value, status_updated_at, evidence_id),
        )

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> Evidence:
        data = dict(row)
        data["private_fields"] = _loads(data["private_fields"])
        data["status"] = EvidenceStatus(data["status"])
        return Evidence(**data)

    def get_evidence(self, evidence_id: str) -> Optional[Evidence]:
        row = self._query_one("SELECT * FROM evidence WHERE id = ?", (evidence_id,))
        return self._row_to_evidence(row) if row else None

    def get_evidence_by_hash(self, content_hash: str) -> Optional[Evidence]:
        row = self._query_one(
            "SELECT * FROM evidence WHERE content_hash = ?", (content_hash,)
        )
        return self._row_to_evidence(row) if row else None

    def list_evidence(self, case_id: str) -> list[Evidence]:
        rows = self._query_all(
            "SELECT * FROM evidence WHERE case_id = ? ORDER BY submitted_at, id",
            (case_id,),
        )
        return [self._row_to_evidence(r) for r in rows]

    # ---------------------------------------------------------------- minutes
    def insert_minute(self, minute: Minute) -> None:
        self._execute(
            """INSERT INTO minutes (id, case_id, title, content, version,
                   created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (minute.id, minute.case_id, minute.title, minute.content,
             minute.version, minute.created_by, minute.created_at, minute.updated_at),
        )

    def update_minute(self, minute: Minute) -> None:
        self._execute(
            "UPDATE minutes SET content = ?, version = ?, updated_at = ? WHERE id = ?",
            (minute.content, minute.version, minute.updated_at, minute.id),
        )

    def get_minute(self, minute_id: str) -> Optional[Minute]:
        row = self._query_one("SELECT * FROM minutes WHERE id = ?", (minute_id,))
        return Minute(**dict(row)) if row else None

    def list_minutes(self, case_id: str) -> list[Minute]:
        rows = self._query_all(
            "SELECT * FROM minutes WHERE case_id = ? ORDER BY created_at", (case_id,)
        )
        return [Minute(**dict(r)) for r in rows]

    def insert_minute_version(self, mv: MinuteVersion) -> None:
        self._execute(
            """INSERT INTO minute_versions (minute_id, version, content,
                   edited_by, edited_at) VALUES (?, ?, ?, ?, ?)""",
            (mv.minute_id, mv.version, mv.content, mv.edited_by, mv.edited_at),
        )

    def list_minute_versions(self, minute_id: str) -> list[MinuteVersion]:
        rows = self._query_all(
            "SELECT * FROM minute_versions WHERE minute_id = ? ORDER BY version",
            (minute_id,),
        )
        return [MinuteVersion(**dict(r)) for r in rows]

    # -------------------------------------------------------------- proposals
    def insert_proposal(self, proposal: Proposal) -> None:
        self._execute(
            """INSERT INTO proposals (id, case_id, content, proposed_by, status,
                   created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (proposal.id, proposal.case_id, proposal.content, proposal.proposed_by,
             proposal.status.value, proposal.created_at, proposal.resolved_at),
        )
        for party_id, decision in proposal.confirmations.items():
            self._execute(
                """INSERT INTO confirmations (proposal_id, party_id, decision,
                       decided_at) VALUES (?, ?, ?, ?)""",
                (proposal.id, party_id, decision.value, proposal.created_at),
            )

    def update_proposal(self, proposal: Proposal) -> None:
        self._execute(
            "UPDATE proposals SET status = ?, resolved_at = ? WHERE id = ?",
            (proposal.status.value, proposal.resolved_at, proposal.id),
        )

    def upsert_confirmation(self, proposal_id: str, party_id: str,
                            decision: Decision, decided_at: str) -> None:
        self._execute(
            """INSERT INTO confirmations (proposal_id, party_id, decision, decided_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (proposal_id, party_id)
               DO UPDATE SET decision = excluded.decision,
                             decided_at = excluded.decided_at""",
            (proposal_id, party_id, decision.value, decided_at),
        )

    def get_proposal(self, proposal_id: str) -> Optional[Proposal]:
        row = self._query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        return self._row_to_proposal(row) if row else None

    def _row_to_proposal(self, row: sqlite3.Row) -> Proposal:
        data = dict(row)
        data["status"] = ProposalStatus(data["status"])
        confirmations = self._query_all(
            "SELECT party_id, decision FROM confirmations WHERE proposal_id = ?",
            (data["id"],),
        )
        data["confirmations"] = {
            c["party_id"]: Decision(c["decision"]) for c in confirmations
        }
        return Proposal(**data)

    def list_proposals(self, case_id: str) -> list[Proposal]:
        rows = self._query_all(
            "SELECT * FROM proposals WHERE case_id = ? ORDER BY created_at", (case_id,)
        )
        return [self._row_to_proposal(r) for r in rows]

    # ----------------------------------------------------------- share grants
    def insert_share_grant(self, grant: ShareGrant) -> None:
        self._execute(
            """INSERT INTO share_grants (id, case_id, evidence_id, granted_by,
                   granted_to, status, created_at, expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (grant.id, grant.case_id, grant.evidence_id, grant.granted_by,
             grant.granted_to, grant.status.value, grant.created_at,
             grant.expires_at, grant.revoked_at),
        )

    def update_share_grant(self, grant: ShareGrant) -> None:
        self._execute(
            "UPDATE share_grants SET status = ?, revoked_at = ? WHERE id = ?",
            (grant.status.value, grant.revoked_at, grant.id),
        )

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> ShareGrant:
        data = dict(row)
        data["status"] = GrantStatus(data["status"])
        return ShareGrant(**data)

    def get_share_grant(self, grant_id: str) -> Optional[ShareGrant]:
        row = self._query_one("SELECT * FROM share_grants WHERE id = ?", (grant_id,))
        return self._row_to_grant(row) if row else None

    def list_share_grants(self, case_id: str) -> list[ShareGrant]:
        rows = self._query_all(
            "SELECT * FROM share_grants WHERE case_id = ? ORDER BY created_at",
            (case_id,),
        )
        return [self._row_to_grant(r) for r in rows]

    # ----------------------------------------------------------------- events
    def insert_event(self, event: TimelineEvent) -> None:
        self._execute(
            """INSERT INTO events (id, case_id, event_type, actor, detail,
                   created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (event.id, event.case_id, event.event_type.value, event.actor,
             json.dumps(event.detail, ensure_ascii=False), event.created_at),
        )

    def list_events(self, case_id: str) -> list[TimelineEvent]:
        rows = self._query_all(
            "SELECT * FROM events WHERE case_id = ? ORDER BY created_at, rowid",
            (case_id,),
        )
        result = []
        for r in rows:
            data = dict(r)
            data["event_type"] = EventType(data["event_type"])
            data["detail"] = _loads(data["detail"])
            result.append(TimelineEvent(**data))
        return result
