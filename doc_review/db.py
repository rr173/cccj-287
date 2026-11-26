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

-- A release ticket is an editor-prepared batch. Until it is confirmed, no
-- public version exists; after confirmation every item becomes visible at once.
CREATE TABLE IF NOT EXISTS release_tickets (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'released')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    released_at TEXT
);

-- Content and title are snapshotted when the ticket is prepared. Combined with
-- immutable revision rows, later author edits cannot replace a locked item.
CREATE TABLE IF NOT EXISTS release_items (
    ticket_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (ticket_id, item_index),
    UNIQUE (ticket_id, document_id),
    FOREIGN KEY (ticket_id) REFERENCES release_tickets(id) ON DELETE CASCADE
);

-- Direct references in display order. "ticket" refs resolve to a public version
-- created by the same ticket; "external" refs are pinned to an existing
-- public_version_id at prepare time and never refreshed later.
CREATE TABLE IF NOT EXISTS release_references (
    ticket_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    ref_index INTEGER NOT NULL,
    document_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('ticket', 'external')),
    locked_revision INTEGER NOT NULL,
    locked_public_version_id TEXT,
    PRIMARY KEY (ticket_id, item_index, ref_index),
    FOREIGN KEY (ticket_id) REFERENCES release_tickets(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS public_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ticket_id TEXT NOT NULL,
    release_ord INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES release_tickets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_public_versions_document
    ON public_versions(document_id, release_ord);

-- The ordered, frozen direct references of one public version.
CREATE TABLE IF NOT EXISTS public_version_refs (
    public_version_id TEXT NOT NULL,
    ref_index INTEGER NOT NULL,
    target_public_version_id TEXT NOT NULL,
    PRIMARY KEY (public_version_id, ref_index),
    FOREIGN KEY (public_version_id) REFERENCES public_versions(id) ON DELETE CASCADE
);

-- Retracting one explicit public version (not the document). Other versions
-- that directly or indirectly reference it become unreadable dynamically.
CREATE TABLE IF NOT EXISTS public_retractions (
    public_version_id TEXT PRIMARY KEY,
    reason TEXT,
    retracted_at TEXT NOT NULL,
    FOREIGN KEY (public_version_id) REFERENCES public_versions(id) ON DELETE CASCADE
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


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add release columns that may be missing in a database created before
    the release feature was introduced.  New databases already have them."""

    expected = {
        "release_references": [("locked_revision", "INTEGER")],
    }
    for table, columns in expected.items():
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            continue
        existing = {
            info["name"]
            for info in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


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


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Give multi-statement public reads one consistent snapshot.

    SQLite WAL readers never block writers, but separate autocommit reads could
    interleave with a concurrent release/retraction commit.  A deferred
    transaction fixes the snapshot for the whole graph walk.
    """

    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")
