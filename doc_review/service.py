"""Database-backed use cases and HTTP response bodies."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import immediate_transaction
from .errors import ApiError
from .mapping import (
    Replacement,
    apply_replacements,
    assert_disjoint,
    map_range_through_revision,
)
from .schemas import DocumentCreate, EditCreate, SuggestionGroupCreate


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class ResponseEnvelope:
    status_code: int
    body: dict[str, Any]
    replay: bool = False


def _error_envelope(
    status_code: int,
    code: str,
    message: str,
    **extra: Any,
) -> ResponseEnvelope:
    error = ApiError(status_code, code, message, **extra)
    return ResponseEnvelope(status_code, {"error": error.to_dict()})


def _store_envelope(
    conn: sqlite3.Connection,
    scope: str,
    request_id_value: str,
    payload_hash: str,
    envelope: ResponseEnvelope,
) -> None:
    conn.execute(
        """
        INSERT INTO request_results (
            scope, request_id, request_hash, status_code, body_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            scope,
            request_id_value,
            payload_hash,
            envelope.status_code,
            json.dumps(envelope.body, ensure_ascii=False, separators=(",", ":")),
            now_iso(),
        ),
    )


def _stored_envelope(
    conn: sqlite3.Connection,
    scope: str,
    request_id_value: str,
    payload_hash: str,
) -> ResponseEnvelope | None:
    row = conn.execute(
        """
        SELECT request_hash, status_code, body_json
        FROM request_results
        WHERE scope = ? AND request_id = ?
        """,
        (scope, request_id_value),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != payload_hash:
        raise ApiError(
            409,
            "idempotency_key_reused",
            "This request_id was already used with different parameters.",
            scope=scope,
            request_id=request_id_value,
        )
    return ResponseEnvelope(
        status_code=row["status_code"],
        body=json.loads(row["body_json"]),
        replay=True,
    )


def _get_document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "document_not_found", f"Document {document_id!r} was not found.")
    return row


def _get_group(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM suggestion_groups WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "suggestion_group_not_found", f"Group {group_id!r} was not found.")
    return row


def _get_revision(
    conn: sqlite3.Connection, document_id: str, revision: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions WHERE document_id = ? AND revision = ?",
        (document_id, revision),
    ).fetchone()


def _revision_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "revision": row["revision"],
        "parent_revision": row["parent_revision"],
        "content": row["content"],
        "source": row["source"],
        "request_id": row["request_id"],
        "group_id": row["group_id"],
        "created_at": row["created_at"],
    }


def _history_operations(
    conn: sqlite3.Connection,
    document_id: str,
    base_revision: int,
    target_revision: int,
) -> list[tuple[int, str, list[Replacement]]]:
    revision_rows = conn.execute(
        """
        SELECT revision, source
        FROM revisions
        WHERE document_id = ? AND revision > ? AND revision <= ?
        ORDER BY revision ASC
        """,
        (document_id, base_revision, target_revision),
    ).fetchall()

    history: list[tuple[int, str, list[Replacement]]] = []
    for revision_row in revision_rows:
        op_rows = conn.execute(
            """
            SELECT start, end, replacement
            FROM revision_ops
            WHERE document_id = ? AND revision = ?
            ORDER BY ord ASC
            """,
            (document_id, revision_row["revision"]),
        ).fetchall()
        ops = [
            Replacement(op["start"], op["end"], op["replacement"])
            for op in op_rows
        ]
        history.append(
            (revision_row["revision"], revision_row["source"], ops)
        )
    return history


def _map_range(
    conn: sqlite3.Connection,
    document_id: str,
    base_revision: int,
    target_revision: int,
    start: int,
    end: int,
) -> dict[str, Any]:
    current_start, current_end = start, end
    conflicts: list[dict[str, Any]] = []

    for revision, source, ops in _history_operations(
        conn, document_id, base_revision, target_revision
    ):
        current_start, current_end, new_conflicts = map_range_through_revision(
            current_start,
            current_end,
            ops,
            revision=revision,
            revision_source=source,
        )
        conflicts.extend(new_conflicts)

    target = _get_revision(conn, document_id, target_revision)
    if target is None:  # defensive; target is always document.current_revision
        raise ApiError(409, "current_revision_missing", "Current revision is missing.")
    content = target["content"]
    current_start = max(0, min(current_start, len(content)))
    current_end = max(current_start, min(current_end, len(content)))

    # A fully deleted anchor can map to a zero-width point. Include a small
    # surrounding codepoint window so callers always have text to re-review.
    context_start = max(0, current_start - 20)
    context_end = min(len(content), current_end + 20)

    return {
        "status": "conflict" if conflicts else "ok",
        "conflict": bool(conflicts),
        "original_range": {"start": start, "end": end},
        "current_range": {"start": current_start, "end": current_end},
        "current_text": content[current_start:current_end],
        "context_range": {"start": context_start, "end": context_end},
        "context_text": content[context_start:context_end],
        "conflicts": conflicts,
    }


def create_document(conn: sqlite3.Connection, payload: DocumentCreate) -> dict[str, Any]:
    document_id = new_id("doc")
    timestamp = now_iso()
    with immediate_transaction(conn):
        conn.execute(
            """
            INSERT INTO documents (id, title, current_revision, created_at)
            VALUES (?, ?, 0, ?)
            """,
            (document_id, payload.title, timestamp),
        )
        conn.execute(
            """
            INSERT INTO revisions (
                document_id, revision, parent_revision, content, source, created_at
            ) VALUES (?, 0, NULL, ?, 'initial', ?)
            """,
            (document_id, payload.content, timestamp),
        )
    return {
        "id": document_id,
        "title": payload.title,
        "current_revision": 0,
        "content": payload.content,
        "created_at": timestamp,
    }


def get_document(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    row = _get_document(conn, document_id)
    revision = _get_revision(conn, document_id, row["current_revision"])
    return {
        "id": row["id"],
        "title": row["title"],
        "current_revision": row["current_revision"],
        "content": revision["content"] if revision else "",
        "created_at": row["created_at"],
    }


def list_revisions(conn: sqlite3.Connection, document_id: str) -> list[dict[str, Any]]:
    _get_document(conn, document_id)
    rows = conn.execute(
        """
        SELECT document_id, revision, parent_revision, source, request_id,
               group_id, created_at, length(content) AS code_point_length
        FROM revisions
        WHERE document_id = ?
        ORDER BY revision ASC
        """,
        (document_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_revision(
    conn: sqlite3.Connection, document_id: str, revision: int
) -> dict[str, Any]:
    _get_document(conn, document_id)
    row = _get_revision(conn, document_id, revision)
    if row is None:
        raise ApiError(
            404,
            "revision_not_found",
            f"Revision {revision} does not exist for document {document_id!r}.",
        )
    return _revision_dict(row)


def submit_edit(
    conn: sqlite3.Connection,
    document_id: str,
    payload: EditCreate,
) -> ResponseEnvelope:
    doc = _get_document(conn, document_id)
    scope = f"edit:{document_id}"
    payload_hash = request_hash(payload.model_dump())

    with immediate_transaction(conn):
        if payload.request_id:
            stored = _stored_envelope(
                conn, scope, payload.request_id, payload_hash
            )
            if stored is not None:
                return stored

        current_revision = doc["current_revision"]
        base_revision = _get_revision(conn, document_id, payload.base_revision)
        if base_revision is None:
            raise ApiError(
                400,
                "invalid_base_revision",
                f"Base revision {payload.base_revision} does not exist.",
                current_revision=current_revision,
            )

        base_content = base_revision["content"]
        if payload.start > len(base_content) or payload.end > len(base_content):
            raise ApiError(
                422,
                "range_outside_revision",
                "The replacement interval is outside the base revision content.",
                base_revision=payload.base_revision,
                content_length=len(base_content),
                submitted_range={"start": payload.start, "end": payload.end},
            )

        if payload.base_revision != current_revision:
            rebased_range = _map_range(
                conn,
                document_id,
                payload.base_revision,
                current_revision,
                payload.start,
                payload.end,
            )
            envelope = _error_envelope(
                409,
                "revision_conflict",
                (
                    f"Edit is based on revision {payload.base_revision}, but current "
                    f"revision is {current_revision}. Rebase using rebased_range and resubmit."
                ),
                document_id=document_id,
                base_revision=payload.base_revision,
                current_revision=current_revision,
                rebased_range=rebased_range,
            )
            if payload.request_id:
                _store_envelope(
                    conn,
                    scope,
                    payload.request_id,
                    payload_hash,
                    envelope,
                )
            return envelope

        op = Replacement(payload.start, payload.end, payload.replacement)
        new_content = apply_replacements(base_content, [op])
        new_revision = current_revision + 1
        timestamp = now_iso()

        conn.execute(
            """
            INSERT INTO revisions (
                document_id, revision, parent_revision, content, source,
                request_id, created_at
            ) VALUES (?, ?, ?, ?, 'edit', ?, ?)
            """,
            (
                document_id,
                new_revision,
                current_revision,
                new_content,
                payload.request_id,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO revision_ops (document_id, revision, ord, start, end, replacement)
            VALUES (?, ?, 0, ?, ?, ?)
            """,
            (
                document_id,
                new_revision,
                payload.start,
                payload.end,
                payload.replacement,
            ),
        )
        conn.execute(
            "UPDATE documents SET current_revision = ? WHERE id = ?",
            (new_revision, document_id),
        )

        body = {
            "document_id": document_id,
            "revision": new_revision,
            "parent_revision": current_revision,
            "base_revision": payload.base_revision,
            "content": new_content,
            "applied_op": {
                "range": {"start": payload.start, "end": payload.end},
                "replacement": payload.replacement,
            },
            "created_at": timestamp,
        }
        envelope = ResponseEnvelope(201, body)
        if payload.request_id:
            _store_envelope(
                conn, scope, payload.request_id, payload_hash, envelope
            )
        return envelope


def _suggestion_rows(payload: SuggestionGroupCreate) -> list[dict[str, Any]]:
    return [
        {
            "index": index,
            "client_id": item.client_id,
            "start": item.start,
            "end": item.end,
            "replacement": item.replacement,
        }
        for index, item in enumerate(payload.suggestions)
    ]


def _map_suggestion_rows(
    conn: sqlite3.Connection,
    document_id: str,
    base_revision: int,
    target_revision: int,
    rows: list[dict[str, Any] | sqlite3.Row],
    base_content: str | None = None,
) -> list[dict[str, Any]]:
    mapped_items: list[dict[str, Any]] = []
    for row in rows:
        index = row["item_index"] if isinstance(row, sqlite3.Row) else row["index"]
        client_id = row["client_item_id"] if isinstance(row, sqlite3.Row) else row.get("client_id")
        start = row["start"]
        end = row["end"]
        replacement = row["replacement"]

        mapping = _map_range(
            conn,
            document_id,
            base_revision,
            target_revision,
            start,
            end,
        )
        item: dict[str, Any] = {
            "index": index,
            "client_id": client_id,
            "original_range": {"start": start, "end": end},
            "original_text": (
                base_content[start:end] if base_content is not None else None
            ),
            "replacement": replacement,
            **mapping,
        }
        if base_content is None:
            item.pop("original_text", None)
        mapped_items.append(item)
    return mapped_items


def create_suggestion_group(
    conn: sqlite3.Connection,
    document_id: str,
    payload: SuggestionGroupCreate,
) -> ResponseEnvelope:
    doc = _get_document(conn, document_id)
    scope = f"create-group:{document_id}"
    payload_hash = request_hash(payload.model_dump())

    try:
        assert_disjoint(
            [(item.start, item.end) for item in payload.suggestions],
            require_nonempty=True,
        )
    except ValueError as exc:
        raise ApiError(
            422,
            "invalid_suggestion_ranges",
            str(exc),
        ) from exc

    with immediate_transaction(conn):
        if payload.request_id:
            stored = _stored_envelope(
                conn, scope, payload.request_id, payload_hash
            )
            if stored is not None:
                return stored

        current_revision = doc["current_revision"]
        base_revision_row = _get_revision(
            conn, document_id, payload.base_revision
        )
        if base_revision_row is None:
            raise ApiError(
                400,
                "invalid_base_revision",
                f"Base revision {payload.base_revision} does not exist.",
                current_revision=current_revision,
            )

        base_content = base_revision_row["content"]
        invalid_items = [
            {
                "index": index,
                "range": {"start": item.start, "end": item.end},
                "content_length": len(base_content),
            }
            for index, item in enumerate(payload.suggestions)
            if item.end > len(base_content)
        ]
        if invalid_items:
            raise ApiError(
                422,
                "range_outside_revision",
                "One or more suggestion ranges are outside the base revision.",
                base_revision=payload.base_revision,
                invalid_items=invalid_items,
            )

        rows = _suggestion_rows(payload)
        item_statuses = _map_suggestion_rows(
            conn,
            document_id,
            payload.base_revision,
            current_revision,
            rows,
            base_content,
        )
        drift_clean = all(not item["conflict"] for item in item_statuses)

        if payload.strict and not drift_clean:
            envelope = _error_envelope(
                409,
                "suggestion_drift_conflict",
                (
                    "One or more suggestions cannot be cleanly located between "
                    f"revision {payload.base_revision} and current revision "
                    f"{current_revision}; re-review the reported current ranges."
                ),
                document_id=document_id,
                base_revision=payload.base_revision,
                current_revision=current_revision,
                drift_clean=False,
                items=item_statuses,
            )
            if payload.request_id:
                _store_envelope(
                    conn, scope, payload.request_id, payload_hash, envelope
                )
            return envelope

        group_id = new_id("grp")
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO suggestion_groups (
                id, document_id, base_revision, status, created_at
            ) VALUES (?, ?, ?, 'pending', ?)
            """,
            (group_id, document_id, payload.base_revision, timestamp),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO suggestion_items (
                    group_id, item_index, client_item_id, start, end, replacement
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    row["index"],
                    row["client_id"],
                    row["start"],
                    row["end"],
                    row["replacement"],
                ),
            )

        body = {
            "id": group_id,
            "document_id": document_id,
            "base_revision": payload.base_revision,
            "status": "pending",
            "current_revision": current_revision,
            "drift_clean": drift_clean,
            "strict": payload.strict,
            "items": item_statuses,
            "created_at": timestamp,
        }
        envelope = ResponseEnvelope(201, body)
        if payload.request_id:
            _store_envelope(
                conn, scope, payload.request_id, payload_hash, envelope
            )
        return envelope


def list_groups(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    _get_document(conn, document_id)
    rows = conn.execute(
        """
        SELECT id, document_id, base_revision, status, accepted_revision,
               winning_request_id, created_at, resolved_at
        FROM suggestion_groups
        WHERE document_id = ?
        ORDER BY created_at ASC, rowid ASC
        """,
        (document_id,),
    ).fetchall()
    return {"document_id": document_id, "groups": [dict(row) for row in rows]}


def _stored_accept_result(
    conn: sqlite3.Connection, group: sqlite3.Row
) -> dict[str, Any] | None:
    request_id_value = group["winning_request_id"]
    if not request_id_value:
        return None
    row = conn.execute(
        """
        SELECT body_json FROM request_results
        WHERE scope = ? AND request_id = ? AND status_code = 200
        """,
        (f"accept:{group['id']}", request_id_value),
    ).fetchone()
    return json.loads(row["body_json"]) if row else None


def get_group(conn: sqlite3.Connection, group_id: str) -> dict[str, Any]:
    group = _get_group(conn, group_id)
    item_rows = conn.execute(
        """
        SELECT group_id, item_index, client_item_id, start, end, replacement
        FROM suggestion_items
        WHERE group_id = ?
        ORDER BY item_index ASC
        """,
        (group_id,),
    ).fetchall()

    body: dict[str, Any] = {
        "id": group["id"],
        "document_id": group["document_id"],
        "base_revision": group["base_revision"],
        "status": group["status"],
        "accepted_revision": group["accepted_revision"],
        "winning_request_id": group["winning_request_id"],
        "close_reason": group["close_reason"],
        "created_at": group["created_at"],
        "resolved_at": group["resolved_at"],
        "suggestions": [
            {
                "index": row["item_index"],
                "client_id": row["client_item_id"],
                "original_range": {"start": row["start"], "end": row["end"]},
                "replacement": row["replacement"],
            }
            for row in item_rows
        ],
    }

    if group["status"] == "accepted":
        body["result"] = _stored_accept_result(conn, group)
    else:
        # Pending and closed groups can both be inspected against current text;
        # only pending groups are allowed to reach acceptance.
        doc = _get_document(conn, group["document_id"])
        current_revision = doc["current_revision"]
        base_revision = _get_revision(
            conn, group["document_id"], group["base_revision"]
        )
        base_content = base_revision["content"] if base_revision else None
        items = _map_suggestion_rows(
            conn,
            group["document_id"],
            group["base_revision"],
            current_revision,
            item_rows,
            base_content,
        )
        body["current_revision"] = current_revision
        body["drift_clean"] = all(not item["conflict"] for item in items)
        body["items"] = items

    return body


def get_accept_result(
    conn: sqlite3.Connection, group_id: str
) -> dict[str, Any]:
    group = _get_group(conn, group_id)
    result = _stored_accept_result(conn, group)
    if result is None:
        raise ApiError(
            404,
            "accept_result_not_found",
            "This group has no successful accepted result.",
            status=group["status"],
        )
    return result


def accept_group(
    conn: sqlite3.Connection,
    group_id: str,
    request_id_value: str,
) -> ResponseEnvelope:
    scope = f"accept:{group_id}"
    payload_hash = request_hash({"request_id": request_id_value})

    with immediate_transaction(conn):
        # A retried request, including concurrent duplicate retries after lock
        # acquisition, must receive the exact first response.
        stored = _stored_envelope(
            conn, scope, request_id_value, payload_hash
        )
        if stored is not None:
            return stored

        group = _get_group(conn, group_id)
        document_id = group["document_id"]
        doc = _get_document(conn, document_id)
        current_revision = doc["current_revision"]

        if group["status"] != "pending":
            code = (
                "suggestion_group_already_accepted"
                if group["status"] == "accepted"
                else "suggestion_group_closed"
            )
            message = (
                "Suggestion group was already accepted; only one acceptance can land."
                if group["status"] == "accepted"
                else "Suggestion group was closed and cannot be accepted."
            )
            extra: dict[str, Any] = {
                "group_id": group_id,
                "status": group["status"],
                "base_revision": group["base_revision"],
                "current_revision": current_revision,
                "accepted_revision": group["accepted_revision"],
                "winning_request_id": group["winning_request_id"],
            }
            if group["status"] == "accepted":
                extra["first_result"] = _stored_accept_result(conn, group)
            envelope = _error_envelope(409, code, message, **extra)
            _store_envelope(
                conn,
                scope,
                request_id_value,
                payload_hash,
                envelope,
            )
            return envelope

        item_rows = conn.execute(
            """
            SELECT group_id, item_index, client_item_id, start, end, replacement
            FROM suggestion_items
            WHERE group_id = ?
            ORDER BY item_index ASC
            """,
            (group_id,),
        ).fetchall()
        base_revision_row = _get_revision(
            conn, document_id, group["base_revision"]
        )
        base_content = base_revision_row["content"] if base_revision_row else None
        items = _map_suggestion_rows(
            conn,
            document_id,
            group["base_revision"],
            current_revision,
            item_rows,
            base_content,
        )

        if any(item["conflict"] for item in items):
            conflict_indexes = [
                item["index"] for item in items if item["conflict"]
            ]
            envelope = _error_envelope(
                409,
                "suggestion_conflicts",
                (
                    "At least one suggestion no longer maps cleanly. The entire "
                    "group was rejected and the document was not changed."
                ),
                group_id=group_id,
                accepted=False,
                status="pending",
                base_revision=group["base_revision"],
                current_revision=current_revision,
                conflict_indexes=conflict_indexes,
                items=items,
            )
            _store_envelope(
                conn,
                scope,
                request_id_value,
                payload_hash,
                envelope,
            )
            return envelope

        current_revision_row = _get_revision(
            conn, document_id, current_revision
        )
        current_content = current_revision_row["content"]
        mapped_ops = [
            Replacement(
                item["current_range"]["start"],
                item["current_range"]["end"],
                item["replacement"],
            )
            for item in items
        ]
        assert_disjoint(mapped_ops, require_nonempty=True)
        new_content = apply_replacements(current_content, mapped_ops)
        new_revision = current_revision + 1
        timestamp = now_iso()

        conn.execute(
            """
            INSERT INTO revisions (
                document_id, revision, parent_revision, content, source,
                request_id, group_id, created_at
            ) VALUES (?, ?, ?, ?, 'accept', ?, ?, ?)
            """,
            (
                document_id,
                new_revision,
                current_revision,
                new_content,
                request_id_value,
                group_id,
                timestamp,
            ),
        )

        ordered_for_storage = sorted(
            zip(items, mapped_ops),
            key=lambda pair: (pair[1].start, pair[1].end),
        )
        cumulative_delta = 0
        applied_ranges: dict[int, dict[str, int]] = {}
        for ordinal, (item, op) in enumerate(ordered_for_storage):
            conn.execute(
                """
                INSERT INTO revision_ops (
                    document_id, revision, ord, start, end, replacement
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    new_revision,
                    ordinal,
                    op.start,
                    op.end,
                    op.replacement,
                ),
            )
            applied_start = op.start + cumulative_delta
            applied_end = applied_start + len(op.replacement)
            applied_ranges[item["index"]] = {
                "start": applied_start,
                "end": applied_end,
            }
            cumulative_delta += op.delta

        conn.execute(
            "UPDATE documents SET current_revision = ? WHERE id = ?",
            (new_revision, document_id),
        )
        updated = conn.execute(
            """
            UPDATE suggestion_groups
            SET status = 'accepted',
                accepted_revision = ?,
                winning_request_id = ?,
                resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (new_revision, request_id_value, timestamp, group_id),
        )
        if updated.rowcount != 1:
            # The immediate write transaction makes this nearly impossible, but
            # keep the compare-and-set guard explicit.
            raise ApiError(
                409,
                "suggestion_group_state_changed",
                "Group status changed while accepting.",
            )

        response_items: list[dict[str, Any]] = []
        for item in items:
            applied_range = applied_ranges[item["index"]]
            response_item = dict(item)
            response_item["located_range"] = item["current_range"]
            response_item["located_context_range"] = item["context_range"]
            response_item["located_context_text"] = item["context_text"]
            response_item["applied_range"] = applied_range
            # The response's current revision is the newly accepted revision.
            response_item["current_range"] = applied_range
            response_item["current_text"] = item["replacement"]
            context_start = max(0, applied_range["start"] - 20)
            context_end = min(len(new_content), applied_range["end"] + 20)
            response_item["context_range"] = {
                "start": context_start,
                "end": context_end,
            }
            response_item["context_text"] = new_content[context_start:context_end]
            response_items.append(response_item)

        body = {
            "accepted": True,
            "group_id": group_id,
            "request_id": request_id_value,
            "document_id": document_id,
            "base_revision": group["base_revision"],
            "parent_revision": current_revision,
            "revision": new_revision,
            "current_revision": new_revision,
            "content": new_content,
            "items": response_items,
            "resolved_at": timestamp,
        }
        envelope = ResponseEnvelope(200, body)
        _store_envelope(
            conn,
            scope,
            request_id_value,
            payload_hash,
            envelope,
        )
        return envelope


def close_group(
    conn: sqlite3.Connection,
    group_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    with immediate_transaction(conn):
        group = _get_group(conn, group_id)
        if group["status"] == "closed":
            return get_group(conn, group_id)
        if group["status"] == "accepted":
            raise ApiError(
                409,
                "suggestion_group_already_accepted",
                "An accepted group cannot be closed.",
                group_id=group_id,
                accepted_revision=group["accepted_revision"],
                winning_request_id=group["winning_request_id"],
            )

        timestamp = now_iso()
        conn.execute(
            """
            UPDATE suggestion_groups
            SET status = 'closed', close_reason = ?, resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (reason, timestamp, group_id),
        )
        # Reason is persisted in suggestion_groups.close_reason.
        return get_group(conn, group_id)
