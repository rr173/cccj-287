"""On-site inspection records, re-review work orders, and pending rounds.

The model in one paragraph
--------------------------
A *field inspection record* says "someone completed check item X on asset A
using this exact public version of the manual".  The registered version is
never overwritten; it stays as the historical evidence.  The record's
*current basis* starts as that registered version and moves to the version used
by the most recent successful re-review.

A record becomes *pending* (needs re-checking) only when its current basis
becomes unreadable because the basis itself, or a public version it directly or
indirectly fixes as a reference, was retracted.  A pending record has exactly
one *open round*.  Adding another retraction reason to the same basis mutates
that round instead of opening a new one.  Publishing new versions does nothing,
and retracting an already-replaced old basis does nothing, so neither can
silently close or reopen a record.

A *work order* is scoped to (asset, check item, manual document) and snapshots
every round that is open for that scope at creation time.  Completion names one
still-readable exact public version of the same manual document; only untouched
snapshotted rounds are resolved, and those records adopt the named version as
their new current basis.  Rounds opened after the order - or reopened for a
different basis after another order closed them - are deliberately not part of
the old order.

Retraction never scans every inspection record: the release code supplies the
set of public versions that just became unreadable and ``apply_retraction``
joins that set against the indexed ``current_basis_public_version_id`` column.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .db import immediate_transaction
from .errors import ApiError
from .schemas import (
    InspectionCreate,
    WorkOrderCompleteRequest,
    WorkOrderCreate,
)
from .service import (
    ResponseEnvelope,
    _stored_envelope,
    _store_envelope,
    new_id,
    now_iso,
    request_hash,
)


# ---------------------------------------------------------------------------
# Lookups / serialisation
# ---------------------------------------------------------------------------


def _blocked_versions(conn: sqlite3.Connection, root: str) -> list[str]:
    # Imported lazily: release imports this module to keep retraction atomic.
    from .release import blocked_versions

    return blocked_versions(conn, [root])


def _public_version_or_404(
    conn: sqlite3.Connection, public_version_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM public_versions WHERE id = ?", (public_version_id,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "public_version_not_found",
            f"Public version {public_version_id!r} was not found.",
        )
    return row


def _trigger_ids(round_row: sqlite3.Row) -> list[str]:
    try:
        value = json.loads(round_row["trigger_public_version_ids_json"])
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _round_body(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "round_id": row["id"],
        "record_id": row["record_id"],
        "status": row["status"],
        "basis_public_version_id": row["basis_public_version_id"],
        "retracted_public_version_ids": _trigger_ids(row),
        "opened_at": row["opened_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by_work_order_id": row["resolved_by_work_order_id"],
    }


def _record_row(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM inspection_records WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "inspection_record_not_found",
            f"Inspection record {record_id!r} was not found.",
        )
    return row


def _open_round(
    conn: sqlite3.Connection, record_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM inspection_pending_rounds
        WHERE record_id = ? AND status = 'open'
        """,
        (record_id,),
    ).fetchone()


def _record_body(
    conn: sqlite3.Connection, row: sqlite3.Row, include_reviews: bool = False
) -> dict[str, Any]:
    open_round = _open_round(conn, row["id"])
    basis = row["current_basis_public_version_id"]
    blocked = _blocked_versions(conn, basis)
    body: dict[str, Any] = {
        "record_id": row["id"],
        "asset_id": row["asset_id"],
        "check_item": row["check_item"],
        "manual_document_id": row["manual_document_id"],
        "registered_public_version_id": row["registered_public_version_id"],
        "current_basis_public_version_id": basis,
        "current_basis_readable": not blocked,
        "current_basis_blocked_public_version_ids": blocked,
        "status": "pending_review" if open_round is not None else "current",
        "pending": open_round is not None,
        "pending_round": _round_body(open_round) if open_round is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_reviews:
        reviews = conn.execute(
            """
            SELECT * FROM inspection_reviews
            WHERE record_id = ?
            ORDER BY reviewed_at ASC, rowid ASC
            """,
            (row["id"],),
        ).fetchall()
        body["reviews"] = [
            {
                "review_id": review["id"],
                "work_order_id": review["work_order_id"],
                "round_id": review["round_id"],
                "public_version_id": review["public_version_id"],
                "reviewed_at": review["reviewed_at"],
            }
            for review in reviews
        ]
    return body


# ---------------------------------------------------------------------------
# Registering a completed inspection
# ---------------------------------------------------------------------------


def register_inspection(
    conn: sqlite3.Connection, payload: InspectionCreate
) -> ResponseEnvelope:
    scope = "inspection-register"
    payload_hash = request_hash(payload.model_dump())

    with immediate_transaction(conn):
        if payload.request_id:
            stored = _stored_envelope(conn, scope, payload.request_id, payload_hash)
            if stored is not None:
                return stored

        version = _public_version_or_404(conn, payload.public_version_id)
        blocked = _blocked_versions(conn, version["id"])
        if blocked:
            raise ApiError(
                409,
                "inspection_basis_unreadable",
                (
                    "The registered public version is not readable because it, "
                    "or a version it directly or indirectly fixes, was retracted."
                ),
                public_version_id=version["id"],
                blocked_public_version_ids=blocked,
            )

        record_id = new_id("insp")
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO inspection_records (
                id, asset_id, check_item, manual_document_id,
                registered_public_version_id, current_basis_public_version_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                payload.asset_id,
                payload.check_item,
                version["document_id"],
                version["id"],
                version["id"],
                timestamp,
                timestamp,
            ),
        )
        body = _record_body(conn, _record_row(conn, record_id))
        envelope = ResponseEnvelope(201, body)
        if payload.request_id:
            _store_envelope(
                conn, scope, payload.request_id, payload_hash, envelope
            )
        return envelope


def list_inspections(
    conn: sqlite3.Connection,
    *,
    asset_id: str | None = None,
    check_item: str | None = None,
    manual_document_id: str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("asset_id", asset_id),
        ("check_item", check_item),
        ("manual_document_id", manual_document_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT * FROM inspection_records
        {where}
        ORDER BY created_at ASC, rowid ASC
        """,
        params,
    ).fetchall()
    return {
        "records": [_record_body(conn, row) for row in rows],
        "count": len(rows),
    }


def get_inspection(conn: sqlite3.Connection, record_id: str) -> dict[str, Any]:
    return _record_body(conn, _record_row(conn, record_id), include_reviews=True)


def list_pending(
    conn: sqlite3.Connection,
    *,
    asset_id: str | None = None,
    check_item: str | None = None,
    manual_document_id: str | None = None,
) -> dict[str, Any]:
    """Current pending records plus the retracted versions that unreadable them."""

    clauses = ["p.status = 'open'"]
    params: list[Any] = []
    for column, value in (
        ("r.asset_id", asset_id),
        ("r.check_item", check_item),
        ("r.manual_document_id", manual_document_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    rows = conn.execute(
        f"""
        SELECT r.*, p.id AS round_id
        FROM inspection_records r
        JOIN inspection_pending_rounds p
          ON p.record_id = r.id AND p.status = 'open'
        WHERE {' AND '.join(clauses)}
        ORDER BY r.created_at ASC, r.rowid ASC
        """,
        params,
    ).fetchall()

    pending: list[dict[str, Any]] = []
    for row in rows:
        # row carries the extra ``round_id`` alias; _record_body reads only the
        # inspection_records columns it needs and adds the open round itself.
        pending.append(_record_body(conn, row))
    return {
        "pending": pending,
        "count": len(pending),
        "unreadable_bases": [
            {
                "record_id": entry["record_id"],
                "current_basis_public_version_id": entry[
                    "current_basis_public_version_id"
                ],
                "retracted_public_version_ids": entry["pending_round"][
                    "retracted_public_version_ids"
                ],
            }
            for entry in pending
            if entry["pending_round"] is not None
        ],
    }


# ---------------------------------------------------------------------------
# Retraction hook
# ---------------------------------------------------------------------------


def apply_retraction(
    conn: sqlite3.Connection,
    newly_unreadable_ids: Iterable[str],
    timestamp: str,
) -> list[str]:
    """Open or extend pending rounds for records whose basis just went dark.

    ``newly_unreadable_ids`` contains the retracted version itself plus every
    version that transitively fixes it (as computed by the release module).
    The join below is an indexed lookup; it never walks the inspection table
    record by record to discover relevance.
    """

    scope_ids = sorted(set(newly_unreadable_ids))
    if not scope_ids:
        return []

    # One indexed lookup per *affected public version* - the retraction closure,
    # which is small - rather than iterating every inspection record.  A CROSS
    # JOIN order hint keeps SQLite from choosing a full scan of the record
    # table's index.
    rows = conn.execute(
        f"""
        SELECT r.id, r.current_basis_public_version_id AS basis
        FROM (SELECT public_version_id FROM (
                {" UNION ALL ".join("SELECT ? AS public_version_id" for _ in scope_ids)}
              )) s
        CROSS JOIN inspection_records r INDEXED BY idx_inspection_records_basis
        WHERE r.current_basis_public_version_id = s.public_version_id
        ORDER BY r.created_at ASC, r.rowid ASC
        """,
        scope_ids,
    ).fetchall()
    if not rows:
        return []

    blocked_by_basis: dict[str, list[str]] = {}
    for row in rows:
        basis = row["basis"]
        if basis not in blocked_by_basis:
            blocked_by_basis[basis] = _blocked_versions(conn, basis)

    touched: list[str] = []
    for row in rows:
        basis = row["basis"]
        blocked = blocked_by_basis.get(basis) or []
        if not blocked:
            # Defensive: a retraction must make its reachable bases unreadable.
            continue
        existing = _open_round(conn, row["id"])
        if existing is not None:
            # Same basis, additional retraction cause: extend, never re-open.
            merged = _trigger_ids(existing)
            for version_id in blocked:
                if version_id not in merged:
                    merged.append(version_id)
            conn.execute(
                """
                UPDATE inspection_pending_rounds
                SET trigger_public_version_ids_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                    existing["id"],
                ),
            )
            touched.append(existing["id"])
            continue

        round_id = new_id("rnd")
        conn.execute(
            """
            INSERT INTO inspection_pending_rounds (
                id, record_id, status, basis_public_version_id,
                trigger_public_version_ids_json, opened_at
            ) VALUES (?, ?, 'open', ?, ?, ?)
            """,
            (
                round_id,
                row["id"],
                basis,
                json.dumps(blocked, ensure_ascii=False, separators=(",", ":")),
                timestamp,
            ),
        )
        conn.execute(
            "UPDATE inspection_records SET updated_at = ? WHERE id = ?",
            (timestamp, row["id"]),
        )
        touched.append(round_id)
    return touched


# ---------------------------------------------------------------------------
# Work orders
# ---------------------------------------------------------------------------


def _captured_items(conn: sqlite3.Connection, work_order_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM review_work_order_items
        WHERE work_order_id = ?
        ORDER BY rowid ASC
        """,
        (work_order_id,),
    ).fetchall()


def _work_order_row(conn: sqlite3.Connection, work_order_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM review_work_orders WHERE id = ?", (work_order_id,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "review_work_order_not_found",
            f"Review work order {work_order_id!r} was not found.",
        )
    return row


def _captured_body(conn: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
    record = conn.execute(
        "SELECT * FROM inspection_records WHERE id = ?", (item["record_id"],)
    ).fetchone()
    round_row = conn.execute(
        "SELECT * FROM inspection_pending_rounds WHERE id = ?",
        (item["round_id"],),
    ).fetchone()
    return {
        "record_id": item["record_id"],
        "round_id": item["round_id"],
        "round_status": round_row["status"] if round_row is not None else "missing",
        "captured_basis_public_version_id": item["basis_public_version_id"],
        "asset_id": record["asset_id"] if record is not None else None,
        "check_item": record["check_item"] if record is not None else None,
        "manual_document_id": (
            record["manual_document_id"] if record is not None else None
        ),
        "registered_public_version_id": (
            record["registered_public_version_id"] if record is not None else None
        ),
        "current_basis_public_version_id": (
            record["current_basis_public_version_id"] if record is not None else None
        ),
    }


def _work_order_body(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    items = _captured_items(conn, row["id"])
    body: dict[str, Any] = {
        "work_order_id": row["id"],
        "status": row["status"],
        "asset_id": row["asset_id"],
        "check_item": row["check_item"],
        "manual_document_id": row["manual_document_id"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "completion_public_version_id": row["completion_public_version_id"],
        "captured_records": [_captured_body(conn, item) for item in items],
        "captured_count": len(items),
    }
    if row["status"] == "completed" and row["result_json"]:
        body["result"] = json.loads(row["result_json"])
    return body


def open_work_order(
    conn: sqlite3.Connection, payload: WorkOrderCreate
) -> ResponseEnvelope:
    scope = "review-work-order-open"
    payload_hash = request_hash(payload.model_dump())

    with immediate_transaction(conn):
        if payload.request_id:
            stored = _stored_envelope(conn, scope, payload.request_id, payload_hash)
            if stored is not None:
                return stored

        rows = conn.execute(
            """
            SELECT r.id AS record_id, p.id AS round_id,
                   p.basis_public_version_id AS basis
            FROM inspection_records r
            JOIN inspection_pending_rounds p
              ON p.record_id = r.id AND p.status = 'open'
            WHERE r.asset_id = ? AND r.check_item = ? AND r.manual_document_id = ?
            ORDER BY r.created_at ASC, r.rowid ASC
            """,
            (payload.asset_id, payload.check_item, payload.manual_document_id),
        ).fetchall()
        if not rows:
            # No pending work means no work order is opened at all.
            raise ApiError(
                409,
                "no_pending_reviews",
                "There are no pending inspections for this asset, check item, and manual document.",
                asset_id=payload.asset_id,
                check_item=payload.check_item,
                manual_document_id=payload.manual_document_id,
            )

        work_order_id = new_id("wo")
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO review_work_orders (
                id, asset_id, check_item, manual_document_id, status, created_at
            ) VALUES (?, ?, ?, ?, 'open', ?)
            """,
            (
                work_order_id,
                payload.asset_id,
                payload.check_item,
                payload.manual_document_id,
                timestamp,
            ),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO review_work_order_items (
                    work_order_id, record_id, round_id, basis_public_version_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    work_order_id,
                    row["record_id"],
                    row["round_id"],
                    row["basis"],
                ),
            )

        body = _work_order_body(conn, _work_order_row(conn, work_order_id))
        envelope = ResponseEnvelope(201, body)
        if payload.request_id:
            _store_envelope(
                conn, scope, payload.request_id, payload_hash, envelope
            )
        return envelope


def get_work_order(conn: sqlite3.Connection, work_order_id: str) -> dict[str, Any]:
    return _work_order_body(conn, _work_order_row(conn, work_order_id))


def complete_work_order(
    conn: sqlite3.Connection,
    work_order_id: str,
    payload: WorkOrderCompleteRequest,
) -> ResponseEnvelope:
    with immediate_transaction(conn):
        order = _work_order_row(conn, work_order_id)

        # A retry of a completed order returns the stored first result and must
        # not touch any pending round - including one opened after completion.
        if order["status"] == "completed":
            if (
                payload.public_version_id is not None
                and payload.public_version_id
                != order["completion_public_version_id"]
            ):
                raise ApiError(
                    409,
                    "review_work_order_already_completed",
                    (
                        "This work order was completed with a different review "
                        "public version; retrying must use the original value."
                    ),
                    work_order_id=work_order_id,
                    completion_public_version_id=order[
                        "completion_public_version_id"
                    ],
                )
            return ResponseEnvelope(
                200,
                json.loads(order["result_json"]),
                replay=True,
            )

        if not payload.public_version_id:
            raise ApiError(
                422,
                "review_basis_required",
                "Completion requires the exact public version used for the re-review.",
            )

        version = _public_version_or_404(conn, payload.public_version_id)
        if version["document_id"] != order["manual_document_id"]:
            raise ApiError(
                409,
                "review_basis_wrong_manual",
                (
                    "The review public version belongs to a different document "
                    "than the work order's manual."
                ),
                public_version_id=version["id"],
                public_version_document_id=version["document_id"],
                manual_document_id=order["manual_document_id"],
            )
        blocked = _blocked_versions(conn, version["id"])
        if blocked:
            # Invalid basis: no completion, no closure, order stays open.
            raise ApiError(
                409,
                "review_basis_unreadable",
                (
                    "The review public version is not readable because it, or a "
                    "version it directly or indirectly fixes, was retracted. "
                    "Nothing was completed."
                ),
                public_version_id=version["id"],
                blocked_public_version_ids=blocked,
            )

        timestamp = now_iso()
        closed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for item in _captured_items(conn, work_order_id):
            round_row = conn.execute(
                "SELECT * FROM inspection_pending_rounds WHERE id = ?",
                (item["round_id"],),
            ).fetchone()
            if (
                round_row is None
                or round_row["status"] != "open"
                or round_row["record_id"] != item["record_id"]
            ):
                # Already resolved by another order, or belongs to an older
                # round.  Never close a round this order did not capture.
                skipped.append(
                    {
                        "record_id": item["record_id"],
                        "round_id": item["round_id"],
                        "reason": (
                            "round_resolved"
                            if round_row is not None
                            else "round_missing"
                        ),
                    }
                )
                continue

            conn.execute(
                """
                UPDATE inspection_pending_rounds
                SET status = 'resolved', resolved_at = ?,
                    resolved_by_work_order_id = ?
                WHERE id = ? AND status = 'open'
                """,
                (timestamp, work_order_id, item["round_id"]),
            )
            # One successful re-review moves the record's current basis to the
            # version that was actually used on site.
            conn.execute(
                """
                UPDATE inspection_records
                SET current_basis_public_version_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (version["id"], timestamp, item["record_id"]),
            )
            review_id = new_id("rev")
            conn.execute(
                """
                INSERT INTO inspection_reviews (
                    id, record_id, round_id, work_order_id,
                    public_version_id, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    item["record_id"],
                    item["round_id"],
                    work_order_id,
                    version["id"],
                    timestamp,
                ),
            )
            record = conn.execute(
                "SELECT * FROM inspection_records WHERE id = ?",
                (item["record_id"],),
            ).fetchone()
            closed.append(
                {
                    "record_id": item["record_id"],
                    "round_id": item["round_id"],
                    "asset_id": record["asset_id"],
                    "check_item": record["check_item"],
                    "manual_document_id": record["manual_document_id"],
                    "registered_public_version_id": record[
                        "registered_public_version_id"
                    ],
                    "previous_basis_public_version_id": item[
                        "basis_public_version_id"
                    ],
                    "new_basis_public_version_id": version["id"],
                    "review_id": review_id,
                }
            )

        result = {
            "work_order_id": work_order_id,
            "status": "completed",
            "asset_id": order["asset_id"],
            "check_item": order["check_item"],
            "manual_document_id": order["manual_document_id"],
            "completed_at": timestamp,
            "review_public_version_id": version["id"],
            "captured_count": len(_captured_items(conn, work_order_id)),
            "closed_records": closed,
            "closed_count": len(closed),
            "skipped_records": skipped,
        }
        conn.execute(
            """
            UPDATE review_work_orders
            SET status = 'completed', completed_at = ?,
                completion_public_version_id = ?, result_json = ?
            WHERE id = ? AND status = 'open'
            """,
            (
                timestamp,
                version["id"],
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                work_order_id,
            ),
        )
        return ResponseEnvelope(200, result)


def list_work_orders(
    conn: sqlite3.Connection,
    *,
    asset_id: str | None = None,
    check_item: str | None = None,
    manual_document_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("asset_id", asset_id),
        ("check_item", check_item),
        ("manual_document_id", manual_document_id),
        ("status", status),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT * FROM review_work_orders
        {where}
        ORDER BY created_at ASC, rowid ASC
        """,
        params,
    ).fetchall()
    return {
        "work_orders": [_work_order_body(conn, row) for row in rows],
        "count": len(rows),
    }
