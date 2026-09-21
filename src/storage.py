"""SQLite 持久化层：建表与连接管理。

所有领域状态（含共享授权、会谈纪要版本历史）都落库，
服务重启后从同一数据库文件恢复，状态保持一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    previous_status TEXT,
    mediator_id TEXT,
    close_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    side TEXT NOT NULL,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (case_id, side)
);

CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    raised_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    party_id TEXT NOT NULL REFERENCES parties(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    private_fields TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    status_updated_at TEXT NOT NULL,
    UNIQUE (case_id, party_id, content_hash)
);

CREATE TABLE IF NOT EXISTS minutes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    session_no INTEGER NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS minute_versions (
    id TEXT PRIMARY KEY,
    minutes_id TEXT NOT NULL REFERENCES minutes(id),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    edited_by TEXT NOT NULL,
    edited_at TEXT NOT NULL,
    UNIQUE (minutes_id, version)
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    content TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS proposal_confirmations (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    party_id TEXT NOT NULL REFERENCES parties(id),
    accept INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    UNIQUE (proposal_id, party_id)
);

CREATE TABLE IF NOT EXISTS share_grants (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    evidence_id TEXT REFERENCES evidence(id),
    granted_by TEXT NOT NULL REFERENCES parties(id),
    granted_to TEXT NOT NULL REFERENCES parties(id),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS timeline (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timeline_case ON timeline(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id);
CREATE INDEX IF NOT EXISTS idx_parties_case ON parties(case_id);
CREATE INDEX IF NOT EXISTS idx_grants_case ON share_grants(case_id);
"""


class Database:
    """SQLite 连接封装。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()
