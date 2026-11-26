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

-- A field inspection is a completed check of one asset against one check item,
-- recorded against the exact public version of a manual that was used on site.
-- The registered version is preserved forever; current_basis_public_version_id
-- moves to the version used by the most recent successful re-review.  A record
-- is "pending" (needs re-review) exactly when it has an open pending round.
CREATE TABLE IF NOT EXISTS inspection_records (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    check_item TEXT NOT NULL,
    manual_document_id TEXT NOT NULL,
    registered_public_version_id TEXT NOT NULL,
    current_basis_public_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inspection_records_scope
    ON inspection_records(asset_id, check_item, manual_document_id, created_at);

-- Retraction looks records up by the current basis id.  This index is what
-- keeps a retraction from scanning the whole inspection table.
CREATE INDEX IF NOT EXISTS idx_inspection_records_basis
    ON inspection_records(current_basis_public_version_id);

-- One open round per record.  The round is the unit a work order captures, so
-- a record that is closed and later re-opened gets a *new* round id and is not
-- mistakenly closable by a work order opened during the earlier round.
CREATE TABLE IF NOT EXISTS inspection_pending_rounds (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    basis_public_version_id TEXT NOT NULL,
    trigger_public_version_ids_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by_work_order_id TEXT,
    FOREIGN KEY (record_id) REFERENCES inspection_records(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_pending_open
    ON inspection_pending_rounds(record_id) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_inspection_pending_record
    ON inspection_pending_rounds(record_id, status);

CREATE TABLE IF NOT EXISTS inspection_reviews (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    round_id TEXT,
    work_order_id TEXT NOT NULL,
    public_version_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES inspection_records(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_inspection_reviews_record
    ON inspection_reviews(record_id, reviewed_at);

-- A supervisor work order is opened for one (asset, check item, manual
-- document) and freezes the set of currently pending rounds it covers.
CREATE TABLE IF NOT EXISTS review_work_orders (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    check_item TEXT NOT NULL,
    manual_document_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    completion_public_version_id TEXT,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_work_orders_scope
    ON review_work_orders(asset_id, check_item, manual_document_id, created_at);

-- Captured round ids are snapshotted at open time.  Completion may only close
-- an item while that exact round is still open; a later round for the same
-- record is a different row and is deliberately not part of the old order.
CREATE TABLE IF NOT EXISTS review_work_order_items (
    work_order_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    basis_public_version_id TEXT NOT NULL,
    PRIMARY KEY (work_order_id, record_id),
    FOREIGN KEY (work_order_id) REFERENCES review_work_orders(id) ON DELETE CASCADE
);

-- Offline sync devices. A device has an independent subscription set and a
-- monotonic sequence of reconciliation batches. last_ack_sequence is the
-- newest batch for which the server has accepted the device's confirmation.
CREATE TABLE IF NOT EXISTS sync_devices (
    id TEXT PRIMARY KEY,
    max_page_items INTEGER NOT NULL CHECK (max_page_items BETWEEN 1 AND 100),
    last_ack_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_subscriptions (
    device_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    document_id TEXT NOT NULL,
    PRIMARY KEY (device_id, document_id),
    UNIQUE (device_id, ordinal),
    FOREIGN KEY (device_id) REFERENCES sync_devices(id) ON DELETE CASCADE
);

-- Every offered batch is immutable once created. target_* is the complete
-- replica state that results from applying that batch, not merely the changed
-- rows. Keeping all unacknowledged batches lets a later batch reconcile both
-- never-applied and applied-but-unconfirmed possible device states.
CREATE TABLE IF NOT EXISTS sync_batches (
    device_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    batch_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'acknowledged', 'invalidated')),
    target_signature TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    target_ids_json TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    write_count INTEGER NOT NULL,
    delete_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    PRIMARY KEY (device_id, sequence),
    FOREIGN KEY (device_id) REFERENCES sync_devices(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_batches_device_batch
    ON sync_batches(device_id, batch_id);

CREATE TABLE IF NOT EXISTS sync_batch_pages (
    batch_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    writes_json TEXT NOT NULL,
    delete_ids_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, page_number),
    FOREIGN KEY (batch_id) REFERENCES sync_batches(batch_id) ON DELETE CASCADE
);

-- Persist which pages were successfully served. Confirmation is rejected until
-- every page of a pending batch has been provided by the server.
CREATE TABLE IF NOT EXISTS sync_batch_served_pages (
    batch_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    served_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, page_number),
    FOREIGN KEY (batch_id) REFERENCES sync_batches(batch_id) ON DELETE CASCADE
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
