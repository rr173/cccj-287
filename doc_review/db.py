"""SQLite persistence and transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    content TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('initial', 'edit', 'accept')),
    request_id TEXT,
    group_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (document_id, revision),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS revision_ops (
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    replacement TEXT NOT NULL,
    PRIMARY KEY (document_id, revision, ord),
    FOREIGN KEY (document_id, revision)
        REFERENCES revisions(document_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS suggestion_groups (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'closed')),
    accepted_revision INTEGER,
    winning_request_id TEXT,
    close_reason TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS suggestion_items (
    group_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    client_item_id TEXT,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    replacement TEXT NOT NULL,
    PRIMARY KEY (group_id, item_index),
    FOREIGN KEY (group_id) REFERENCES suggestion_groups(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_groups_document
    ON suggestion_groups(document_id, created_at);

CREATE TABLE IF NOT EXISTS request_results (
    scope TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, request_id)
);
"""


def connect(database_path: str | Path) -> sqlite3.Connection:
    target = str(database_path)
    is_uri = target.startswith("file:")

    if not is_uri and target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        target,
        timeout=30.0,
        isolation_level=None,  # explicit BEGIN IMMEDIATE / COMMIT
        check_same_thread=False,
        uri=is_uri,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if target != ":memory:" and "mode=memory" not in target:
        # WAL lets readers proceed while a writer is committing; all acceptance
        # decisions are still serialized by BEGIN IMMEDIATE.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize write decisions and make retry/race checks authoritative."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")
