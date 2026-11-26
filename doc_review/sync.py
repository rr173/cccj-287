"""HTTP offline-sync use cases.

The protocol is intentionally reconciliation based:

* the client never uploads its local manifest;
* the server stores every offered batch and confirmation;
* a new batch is computed from the current readable public graph and *all*
  device states that may exist because an acknowledgement can be missing;
* pages are immutable and replayable while their batch remains current;
* once publishing, retraction, or a subscription replacement changes the
  target, the prior pending batch is permanently invalidated.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import Any

from .db import immediate_transaction
from .errors import ApiError
from .release import (
    _latest_public_version,
    _public_version_row,
    _ref_ids,
    blocked_versions,
)
from .schemas import DeviceRegisterRequest
from .service import ResponseEnvelope, new_id, now_iso, request_hash


# ---------------------------------------------------------------------------
# Request normalization and small response builders
# ---------------------------------------------------------------------------


def page_limit_from_payload(payload: DeviceRegisterRequest) -> int:
    for name in (
        "max_page_items",
        "max_changes_per_page",
        "max_items_per_page",
        "max_page_size",
        "page_item_limit",
        "items_per_page",
        "per_page",
        "page_limit",
        "page_size",
        "limit",
    ):
        value = getattr(payload, name, None)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ApiError(
                    422,
                    "invalid_page_limit",
                    "The page limit must be an integer from 1 through 100.",
                    field=name,
                    received=value,
                )
            if not 1 <= value <= 100:
                raise ApiError(
                    422,
                    "invalid_page_limit",
                    "The page limit must be from 1 through 100.",
                    field=name,
                    min=1,
                    max=100,
                    received=value,
                )
            return value
    # Generous default; callers are still encouraged to send the limit.
    return 100


def coerce_document_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        for key in (
            "document_ids",
            "documentIds",
            "doc_ids",
            "ids",
            "subscriptions",
            "documents",
            "document_id",
        ):
            if key in value:
                value = value[key]
                break
        else:
            # Allow a single {"document_id": "..."} object.
            if "document_id" in value:
                value = [value["document_id"]]
            else:
                raise ApiError(
                    422,
                    "invalid_subscriptions",
                    "Provide document_ids as a list of document ID strings.",
                )

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ApiError(
            422,
            "invalid_subscriptions",
            "Subscriptions must be a list of document ID strings.",
        )

    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        document_id: Any = item
        if isinstance(item, dict):
            document_id = (
                item.get("document_id")
                or item.get("documentId")
                or item.get("doc_id")
                or item.get("id")
            )
        if not isinstance(document_id, str) or not document_id.strip():
            raise ApiError(
                422,
                "invalid_subscription_document_id",
                "Every subscription must contain a non-empty document ID.",
                index=index,
                item=item,
            )
        document_id = document_id.strip()
        if len(document_id) > 200:
            raise ApiError(
                422,
                "invalid_subscription_document_id",
                "Document IDs must not exceed 200 characters.",
                index=index,
            )
        # It is a set semantically; preserve first occurrence for display.
        if document_id not in seen:
            seen.add(document_id)
            result.append(document_id)
    return result


def _get_device(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sync_devices WHERE id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "sync_device_not_found",
            f"Sync device {device_id!r} is not registered.",
            device_id=device_id,
            register_paths=["/sync/devices", "/sync/devices/register"],
        )
    return row


def _subscription_ids(conn: sqlite3.Connection, device_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT document_id
        FROM sync_subscriptions
        WHERE device_id = ?
        ORDER BY ordinal ASC
        """,
        (device_id,),
    ).fetchall()
    return [row["document_id"] for row in rows]


def device_body(conn: sqlite3.Connection, device: sqlite3.Row) -> dict[str, Any]:
    document_ids = _subscription_ids(conn, device["id"])
    return {
        "device_id": device["id"],
        "id": device["id"],
        "max_page_items": device["max_page_items"],
        "max_items_per_page": device["max_page_items"],
        "max_changes_per_page": device["max_page_items"],
        "max_page_size": device["max_page_items"],
        "page_item_limit": device["max_page_items"],
        "items_per_page": device["max_page_items"],
        "per_page": device["max_page_items"],
        "page_limit": device["max_page_items"],
        "page_size": device["max_page_items"],
        "limit": device["max_page_items"],
        "last_ack_sequence": device["last_ack_sequence"],
        "last_ack_seq": device["last_ack_sequence"],
        "ack_sequence": device["last_ack_sequence"],
        "acknowledged_sequence": device["last_ack_sequence"],
        "confirmed_sequence": device["last_ack_sequence"],
        "subscriptions": document_ids,
        "document_ids": document_ids,
        "created_at": device["created_at"],
        "updated_at": device["updated_at"],
    }


def register_device(
    conn: sqlite3.Connection, payload: DeviceRegisterRequest
) -> ResponseEnvelope:
    page_limit = page_limit_from_payload(payload)
    timestamp = now_iso()

    with immediate_transaction(conn):
        if payload.device_id:
            existing = conn.execute(
                "SELECT * FROM sync_devices WHERE id = ?",
                (payload.device_id,),
            ).fetchone()
            if existing is not None:
                if existing["max_page_items"] != page_limit:
                    raise ApiError(
                        409,
                        "sync_device_already_registered",
                        (
                            "This device ID is already registered with a "
                            "different page limit."
                        ),
                        device_id=payload.device_id,
                        existing_max_page_items=existing["max_page_items"],
                        submitted_max_page_items=page_limit,
                    )
                return ResponseEnvelope(
                    200, device_body(conn, existing), replay=True
                )
            device_id = payload.device_id
        else:
            device_id = new_id("dev")

        conn.execute(
            """
            INSERT INTO sync_devices (
                id, max_page_items, last_ack_sequence, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?)
            """,
            (device_id, page_limit, timestamp, timestamp),
        )

        subscription_input = (
            payload.document_ids
            if payload.document_ids is not None
            else payload.subscriptions
            if payload.subscriptions is not None
            else payload.ids
        )
        if subscription_input is not None:
            initial_document_ids = coerce_document_ids(subscription_input)
            for ordinal, document_id in enumerate(initial_document_ids):
                conn.execute(
                    """
                    INSERT INTO sync_subscriptions (device_id, ordinal, document_id)
                    VALUES (?, ?, ?)
                    """,
                    (device_id, ordinal, document_id),
                )

        device = _get_device(conn, device_id)
        return ResponseEnvelope(201, device_body(conn, device))


def get_device_state(conn: sqlite3.Connection, device_id: str) -> dict[str, Any]:
    with immediate_transaction(conn):
        device = _get_device(conn, device_id)
        return device_body(conn, device)


def replace_subscriptions(
    conn: sqlite3.Connection,
    device_id: str,
    document_ids: list[str],
) -> dict[str, Any]:
    with immediate_transaction(conn):
        device = _get_device(conn, device_id)
        timestamp = now_iso()
        conn.execute(
            "DELETE FROM sync_subscriptions WHERE device_id = ?",
            (device_id,),
        )
        for ordinal, document_id in enumerate(document_ids):
            conn.execute(
                """
                INSERT INTO sync_subscriptions (device_id, ordinal, document_id)
                VALUES (?, ?, ?)
                """,
                (device_id, ordinal, document_id),
            )
        conn.execute(
            "UPDATE sync_devices SET updated_at = ? WHERE id = ?",
            (timestamp, device_id),
        )
        device = _get_device(conn, device_id)
        body = device_body(conn, device)
        # Convenient current mapping; this does not create or return a batch.
        target = _compute_target(conn, device)
        body["current_mapping"] = target["mapping"]
        body["mapping"] = target["mapping_by_document"]
        return body


# ---------------------------------------------------------------------------
# Target graph and reconciliation
# ---------------------------------------------------------------------------


def _compute_target(
    conn: sqlite3.Connection, device: sqlite3.Row
) -> dict[str, Any]:
    """Compute the complete current readable target replica.

    Subscription roots are the current latest public version only when that
    latest version is readable. There is deliberately no fallback to an older
    latest after retraction. All other target versions are reached through
    immutable fixed references.
    """

    document_ids = _subscription_ids(conn, device["id"])
    mapping: list[dict[str, Any]] = []
    roots: list[str] = []

    for document_id in document_ids:
        latest = _latest_public_version(conn, document_id)
        public_version_id: str | None = None
        if latest is not None:
            blocked = blocked_versions(conn, [latest["id"]])
            if not blocked:
                public_version_id = latest["id"]
                roots.append(public_version_id)
        mapping.append(
            {
                "document_id": document_id,
                "public_version_id": public_version_id,
                "readable": public_version_id is not None,
                "included": public_version_id is not None,
            }
        )

    target_ids: set[str] = set()
    queue: deque[str] = deque()
    for root_id in roots:
        if root_id not in target_ids:
            target_ids.add(root_id)
            queue.append(root_id)

    while queue:
        current_id = queue.popleft()
        for reference_id in _ref_ids(conn, current_id):
            if reference_id not in target_ids:
                # References are enforced by release construction/FKs, but a
                # defensive missing row must not produce an unusable batch.
                if _public_version_row(conn, reference_id) is None:
                    continue
                target_ids.add(reference_id)
                queue.append(reference_id)

    ordered_ids = sorted(target_ids)
    signature = request_hash(
        {
            "mapping": sorted(
                (item["document_id"], item["public_version_id"])
                for item in mapping
            ),
            "target_ids": ordered_ids,
        }
    )
    return {
        "mapping": mapping,
        "mapping_by_document": {
            item["document_id"]: item["public_version_id"]
            for item in mapping
            if item["public_version_id"] is not None
        },
        "target_ids": ordered_ids,
        "target_id_set": target_ids,
        "signature": signature,
    }


def _batch_row(
    conn: sqlite3.Connection, device_id: str, sequence: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_batches WHERE device_id = ? AND sequence = ?",
        (device_id, sequence),
    ).fetchone()


def _batch_by_batch_id(
    conn: sqlite3.Connection, batch_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def _state_ids(batch: sqlite3.Row) -> set[str]:
    return set(json.loads(batch["target_ids_json"]))


def _possible_state_sets(
    conn: sqlite3.Connection, device: sqlite3.Row
) -> list[set[str]]:
    """All complete replica states the server must conservatively handle."""

    ack_sequence = device["last_ack_sequence"]
    states: list[set[str]] = []

    if ack_sequence == 0:
        # Registration starts from an empty replica.
        states.append(set())
    else:
        acknowledged = _batch_row(conn, device["id"], ack_sequence)
        if acknowledged is not None:
            states.append(_state_ids(acknowledged))
        else:
            # Defensive fallback: without the acknowledged snapshot, assume
            # nothing is guaranteed rather than omitting required bodies.
            states.append(set())

    rows = conn.execute(
        """
        SELECT b.*,
               (
                   SELECT COUNT(*)
                   FROM sync_batch_served_pages s
                   WHERE s.batch_id = b.batch_id
               ) AS served_page_count
        FROM sync_batches b
        WHERE b.device_id = ? AND b.sequence > ?
        ORDER BY b.sequence ASC
        """,
        (device["id"], ack_sequence),
    ).fetchall()
    for row in rows:
        # A protocol-conformant client can only apply a batch after every page
        # has been provided. Incomplete batches are therefore not possible
        # applied states; excluding them avoids resending versions guaranteed
        # by the device's last acknowledged state.
        if row["served_page_count"] >= row["page_count"]:
            states.append(_state_ids(row))
    return states


def _write_payload(
    conn: sqlite3.Connection, public_version_id: str
) -> dict[str, Any]:
    row = _public_version_row(conn, public_version_id)
    if row is None:
        raise ApiError(
            409,
            "sync_target_version_missing",
            "A target public version disappeared while building a sync batch.",
            public_version_id=public_version_id,
        )

    reference_rows = conn.execute(
        """
        SELECT r.ref_index,
               r.target_public_version_id,
               v.document_id AS target_document_id
        FROM public_version_refs r
        LEFT JOIN public_versions v ON v.id = r.target_public_version_id
        WHERE r.public_version_id = ?
        ORDER BY r.ref_index ASC
        """,
        (public_version_id,),
    ).fetchall()
    reference_ids = [
        ref["target_public_version_id"] for ref in reference_rows
    ]
    reference_details = [
        {
            "ref_index": ref["ref_index"],
            "public_version_id": ref["target_public_version_id"],
            "document_id": ref["target_document_id"],
        }
        for ref in reference_rows
    ]
    return {
        "kind": "write",
        # Several aliases make the key explicit to different client styles.
        "public_version_id": public_version_id,
        "version_id": public_version_id,
        "id": public_version_id,
        "document_id": row["document_id"],
        "revision": row["revision"],
        "title": row["title"],
        "content": row["content"],
        # In the offline sync contract, references are ordered public IDs.
        "references": reference_ids,
        "reference_ids": reference_ids,
        "ordered_references": reference_ids,
        # Rich details remain available for diagnostics/UI mapping.
        "reference_details": reference_details,
    }


def _paginate(
    writes: list[dict[str, Any]],
    delete_ids: list[str],
    page_limit: int,
) -> list[tuple[list[dict[str, Any]], list[str]]]:
    pages: list[tuple[list[dict[str, Any]], list[str]]] = []
    write_index = 0
    delete_index = 0

    while write_index < len(writes) or delete_index < len(delete_ids):
        page_writes: list[dict[str, Any]] = []
        page_deletes: list[str] = []
        used = 0

        while used < page_limit and write_index < len(writes):
            page_writes.append(writes[write_index])
            write_index += 1
            used += 1
        while used < page_limit and delete_index < len(delete_ids):
            page_deletes.append(delete_ids[delete_index])
            delete_index += 1
            used += 1

        pages.append((page_writes, page_deletes))

    # Mapping-only batches still need one page so the client can atomically
    # replace its subscription mapping after collecting a complete batch.
    if not pages:
        pages.append(([], []))
    return pages


def _create_batch(
    conn: sqlite3.Connection,
    device: sqlite3.Row,
    target: dict[str, Any],
) -> sqlite3.Row:
    next_sequence = int(
        conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM sync_batches WHERE device_id = ?",
            (device["id"],),
        ).fetchone()[0]
    )
    batch_id = new_id("syb")
    states = _possible_state_sets(conn, device)
    guaranteed = set.intersection(*states) if states else set()
    possible = set.union(*states) if states else set()

    write_ids = sorted(target["target_id_set"] - guaranteed)
    delete_ids = sorted(possible - target["target_id_set"])
    writes = [_write_payload(conn, version_id) for version_id in write_ids]
    pages = _paginate(writes, delete_ids, device["max_page_items"])
    timestamp = now_iso()

    conn.execute(
        """
        INSERT INTO sync_batches (
            device_id, sequence, batch_id, status, target_signature,
            mapping_json, target_ids_json, page_count, write_count,
            delete_count, created_at
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device["id"],
            next_sequence,
            batch_id,
            target["signature"],
            json.dumps(target["mapping"], ensure_ascii=False, separators=(",", ":")),
            json.dumps(target["target_ids"], separators=(",", ":")),
            len(pages),
            len(writes),
            len(delete_ids),
            timestamp,
        ),
    )

    for page_number, (page_writes, page_deletes) in enumerate(pages, start=1):
        conn.execute(
            """
            INSERT INTO sync_batch_pages (
                batch_id, page_number, writes_json, delete_ids_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                batch_id,
                page_number,
                json.dumps(page_writes, ensure_ascii=False, separators=(",", ":")),
                json.dumps(page_deletes, separators=(",", ":")),
            ),
        )

    return _batch_row(conn, device["id"], next_sequence)  # type: ignore[return-value]


def _mark_page_served(
    conn: sqlite3.Connection, batch_id: str, page_number: int
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO sync_batch_served_pages (
            batch_id, page_number, served_at
        ) VALUES (?, ?, ?)
        """,
        (batch_id, page_number, now_iso()),
    )


def _served_page_numbers(
    conn: sqlite3.Connection, batch: sqlite3.Row
) -> list[int]:
    rows = conn.execute(
        "SELECT page_number FROM sync_batch_served_pages WHERE batch_id = ?",
        (batch["batch_id"],),
    ).fetchall()
    return sorted(row["page_number"] for row in rows)


def _batch_metadata(
    conn: sqlite3.Connection, batch: sqlite3.Row
) -> dict[str, Any]:
    device = _get_device(conn, batch["device_id"])
    mapping = json.loads(batch["mapping_json"])
    target_ids = json.loads(batch["target_ids_json"])
    mapping_by_document = {
        item["document_id"]: item["public_version_id"]
        for item in mapping
        if item.get("public_version_id")
    }
    return {
        "device_id": batch["device_id"],
        "sequence": batch["sequence"],
        "seq": batch["sequence"],
        "batch_sequence": batch["sequence"],
        "cursor": batch["sequence"],
        "to_sequence": batch["sequence"],
        "batch_id": batch["batch_id"],
        "id": batch["batch_id"],
        "status": batch["status"],
        "batch_status": batch["status"],
        "target_signature": batch["target_signature"],
        "subscription_mapping": mapping,
        "mapping": mapping_by_document,
        "mappings": mapping_by_document,
        "target_version_ids": target_ids,
        "page_count": batch["page_count"],
        "page_numbers": list(range(1, batch["page_count"] + 1)),
        "max_page_items": device["max_page_items"],
        "max_items_per_page": device["max_page_items"],
        "page_limit": device["max_page_items"],
        "page_size": device["max_page_items"],
        "write_count": batch["write_count"],
        "total_writes": batch["write_count"],
        "delete_count": batch["delete_count"],
        "total_deletes": batch["delete_count"],
        "total_changes": batch["write_count"] + batch["delete_count"],
        "created_at": batch["created_at"],
        "acknowledged_at": batch["acknowledged_at"],
    }


def _page_payload(
    conn: sqlite3.Connection,
    batch: sqlite3.Row,
    page_number: int,
) -> dict[str, Any]:
    page = conn.execute(
        "SELECT * FROM sync_batch_pages WHERE batch_id = ? AND page_number = ?",
        (batch["batch_id"], page_number),
    ).fetchone()
    if page is None:
        raise ApiError(
            404,
            "sync_page_not_found",
            f"Sync page {page_number} does not exist for this batch.",
            device_id=batch["device_id"],
            sequence=batch["sequence"],
            batch_id=batch["batch_id"],
            page_number=page_number,
            page_count=batch["page_count"],
        )

    writes = json.loads(page["writes_json"])
    delete_ids = json.loads(page["delete_ids_json"])
    writes_by_id = {write["public_version_id"]: write for write in writes}
    delete_operations = [
        {
            "op": "delete",
            "kind": "delete",
            "type": "delete",
            "public_version_id": version_id,
            "version_id": version_id,
            "id": version_id,
        }
        for version_id in delete_ids
    ]
    write_operations: list[dict[str, Any]] = []
    for write in writes:
        operation = dict(write)
        operation["op"] = "put"
        operation["operation"] = "write"
        operation["type"] = "write"
        write_operations.append(operation)
    changes = write_operations + delete_operations
    metadata = _batch_metadata(conn, batch)
    metadata.pop("status", None)  # page envelope uses protocol status below
    metadata.pop("batch_status", None)
    return {
        **metadata,
        "status": "page",
        "page_number": page_number,
        "page": page_number,
        "page_index": page_number - 1,
        "pageIndex": page_number - 1,
        "writes": writes,
        "write_versions": writes,
        "versions_to_write": writes,
        "writes_by_public_version_id": writes_by_id,
        "writes_by_id": writes_by_id,
        "versions": writes_by_id,
        "versions_by_id": writes_by_id,
        "delete_ids": delete_ids,
        "deleted_ids": delete_ids,
        "deleted_version_ids": delete_ids,
        "delete_version_ids": delete_ids,
        "versions_to_delete": delete_ids,
        "deletion_ids": delete_ids,
        "removed_ids": delete_ids,
        "remove_ids": delete_ids,
        "removals": delete_ids,
        "deletes": delete_ids,
        "deletions": delete_ids,
        "delete_operations": delete_operations,
        "write_operations": write_operations,
        "changes": changes,
        "operations": changes,
        "items": changes,
        "page_write_count": len(writes),
        "page_delete_count": len(delete_ids),
        "page_item_count": len(writes) + len(delete_ids),
        "change_count": len(changes),
        "has_more": page_number < batch["page_count"],
        "has_next": page_number < batch["page_count"],
        "next_page": page_number + 1 if page_number < batch["page_count"] else None,
        "next_page_number": page_number + 1 if page_number < batch["page_count"] else None,
        "next_page_index": page_number if page_number < batch["page_count"] else None,
        "previous_page": page_number - 1 if page_number > 1 else None,
        "previous_page_index": page_number - 2 if page_number > 1 else None,
    }


def _in_sync_body(
    conn: sqlite3.Connection, device: sqlite3.Row, target: dict[str, Any]
) -> dict[str, Any]:
    return {
        "status": "in_sync",
        "sync_status": "in_sync",
        "synced": True,
        "synchronized": True,
        "up_to_date": True,
        "device_id": device["id"],
        "id": device["id"],
        "sequence": device["last_ack_sequence"],
        "seq": device["last_ack_sequence"],
        "cursor": device["last_ack_sequence"],
        "last_ack_sequence": device["last_ack_sequence"],
        "last_ack_seq": device["last_ack_sequence"],
        "ack_sequence": device["last_ack_sequence"],
        "acknowledged_sequence": device["last_ack_sequence"],
        "confirmed_sequence": device["last_ack_sequence"],
        "subscription_mapping": target["mapping"],
        "mapping": target["mapping_by_document"],
        "mappings": target["mapping_by_document"],
        "target_version_ids": target["target_ids"],
    }


def _ack_target_matches(
    conn: sqlite3.Connection, device: sqlite3.Row, target: dict[str, Any]
) -> bool:
    ack_sequence = device["last_ack_sequence"]
    if ack_sequence == 0:
        return target["signature"] == request_hash(
            {"mapping": [], "target_ids": []}
        )
    batch = _batch_row(conn, device["id"], ack_sequence)
    return batch is not None and batch["target_signature"] == target["signature"]


def pull_changes(
    conn: sqlite3.Connection, device_id: str
) -> dict[str, Any]:
    with immediate_transaction(conn):
        device = _get_device(conn, device_id)
        target = _compute_target(conn, device)
        latest = conn.execute(
            """
            SELECT * FROM sync_batches
            WHERE device_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()

        batch: sqlite3.Row | None = None
        if latest is not None and latest["sequence"] > device["last_ack_sequence"]:
            if (
                latest["status"] == "pending"
                and latest["target_signature"] == target["signature"]
            ):
                batch = latest
            else:
                if latest["status"] == "pending":
                    conn.execute(
                        "UPDATE sync_batches SET status = 'invalidated' WHERE batch_id = ?",
                        (latest["batch_id"],),
                    )
                batch = _create_batch(conn, device, target)
        elif _ack_target_matches(conn, device, target):
            return _in_sync_body(conn, device, target)
        else:
            batch = _create_batch(conn, device, target)

        _mark_page_served(conn, batch["batch_id"], 1)
        body = _page_payload(conn, batch, 1)
        metadata = _batch_metadata(conn, batch)
        body["status"] = "batch"
        body["sync_status"] = "batch"
        body["synced"] = False
        body["synchronized"] = False
        body["up_to_date"] = False
        body["batch_status"] = metadata["status"]
        body["batch"] = metadata
        body["pull_path"] = f"/sync/devices/{device_id}/pull"
        body["page_path_template"] = (
            f"/sync/devices/{device_id}/batches/{batch['sequence']}/pages/{{page}}"
        )
        body["confirm_path"] = (
            f"/sync/devices/{device_id}/batches/{batch['sequence']}/confirm"
        )
        return body


def _validate_live_batch(
    conn: sqlite3.Connection,
    device: sqlite3.Row,
    batch: sqlite3.Row,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if target is None:
        target = _compute_target(conn, device)

    stale = (
        batch["status"] == "invalidated"
        or batch["target_signature"] != target["signature"]
    )
    if stale and batch["status"] == "pending":
        conn.execute(
            "UPDATE sync_batches SET status = 'invalidated' WHERE batch_id = ?",
            (batch["batch_id"],),
        )
    if stale:
        latest = conn.execute(
            """
            SELECT sequence FROM sync_batches
            WHERE device_id = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (device["id"],),
        ).fetchone()
        raise ApiError(
            409,
            "sync_batch_stale",
            (
                "This sync batch no longer matches the device target. It is "
                "permanently invalid; pull again for a correcting batch."
            ),
            device_id=device["id"],
            sequence=batch["sequence"],
            batch_id=batch["batch_id"],
            batch_status="invalidated",
            latest_sequence=latest["sequence"] if latest else None,
            pull_path=f"/sync/devices/{device['id']}/pull",
            can_revive=False,
        )
    return target


def get_batch_page(
    conn: sqlite3.Connection,
    device_id: str,
    sequence: int,
    page_number: int,
) -> dict[str, Any]:
    with immediate_transaction(conn):
        device = _get_device(conn, device_id)
        batch = _batch_row(conn, device_id, sequence)
        if batch is None:
            raise ApiError(
                404,
                "sync_batch_not_found",
                f"Sync batch sequence {sequence} was not found for this device.",
                device_id=device_id,
                sequence=sequence,
            )

        target = _compute_target(conn, device)
        # Bodies must not be returned for a stale pending batch. Acknowledged
        # batches are also not replayed through pages after the target moves.
        _validate_live_batch(conn, device, batch, target)

        if not 1 <= page_number <= batch["page_count"]:
            raise ApiError(
                404,
                "sync_page_not_found",
                f"Page {page_number} is outside 1..{batch['page_count']}.",
                page_number=page_number,
                page_count=batch["page_count"],
            )

        _mark_page_served(conn, batch["batch_id"], page_number)
        body = _page_payload(conn, batch, page_number)
        body["sync_status"] = "page"
        body["batch"] = _batch_metadata(conn, batch)
        return body


def get_batch_by_id_page(
    conn: sqlite3.Connection, batch_id: str, page_number: int
) -> dict[str, Any]:
    with immediate_transaction(conn):
        batch = _batch_by_batch_id(conn, batch_id)
        if batch is None:
            raise ApiError(
                404,
                "sync_batch_not_found",
                f"Sync batch {batch_id!r} was not found.",
                batch_id=batch_id,
            )
    # Re-enter the device-scoped transaction/path.
    return get_batch_page(
        conn, batch["device_id"], batch["sequence"], page_number
    )


def get_batch_metadata(
    conn: sqlite3.Connection, device_id: str, sequence: int
) -> dict[str, Any]:
    with immediate_transaction(conn):
        device = _get_device(conn, device_id)
        batch = _batch_row(conn, device_id, sequence)
        if batch is None:
            raise ApiError(404, "sync_batch_not_found", "Batch not found.")
        target = _compute_target(conn, device)
        stale = (
            batch["status"] == "invalidated"
            or batch["target_signature"] != target["signature"]
        )
        if stale and batch["status"] == "pending":
            conn.execute(
                "UPDATE sync_batches SET status = 'invalidated' WHERE batch_id = ?",
                (batch["batch_id"],),
            )
            batch = _batch_row(conn, device_id, sequence)
        body = _batch_metadata(conn, batch)  # type: ignore[arg-type]
        body["stale"] = stale
        body["current"] = not stale
        return body


def _ack_body(
    conn: sqlite3.Connection,
    device: sqlite3.Row,
    batch: sqlite3.Row,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "status": "acknowledged",
        "confirmed": True,
        "acknowledged": True,
        "replayed": replayed,
        "device_id": device["id"],
        "sequence": batch["sequence"],
        "seq": batch["sequence"],
        "batch_sequence": batch["sequence"],
        "batch_id": batch["batch_id"],
        "id": batch["batch_id"],
        "last_ack_sequence": device["last_ack_sequence"],
        "last_ack_seq": device["last_ack_sequence"],
        "ack_sequence": device["last_ack_sequence"],
        "acknowledged_sequence": device["last_ack_sequence"],
        "confirmed_sequence": device["last_ack_sequence"],
        "progress_sequence": device["last_ack_sequence"],
        "acknowledged_at": batch["acknowledged_at"],
    }


def confirm_batch(
    conn: sqlite3.Connection,
    device_id: str,
    sequence: int,
) -> ResponseEnvelope:
    with immediate_transaction(conn):
        device = _get_device(conn, device_id)
        batch = _batch_row(conn, device_id, sequence)
        if batch is None:
            raise ApiError(
                404,
                "sync_batch_not_found",
                f"Sync batch sequence {sequence} was not found for this device.",
                device_id=device_id,
                sequence=sequence,
            )

        # A successfully processed confirmation is idempotent even if publishing
        # or retraction has since moved the target. Never roll progress back.
        if batch["status"] == "acknowledged":
            if device["last_ack_sequence"] < sequence:
                conn.execute(
                    "UPDATE sync_devices SET last_ack_sequence = ? WHERE id = ?",
                    (sequence, device_id),
                )
                device = _get_device(conn, device_id)
            return ResponseEnvelope(
                200, _ack_body(conn, device, batch, replayed=True), replay=True
            )

        target = _compute_target(conn, device)
        _validate_live_batch(conn, device, batch, target)

        served = set(_served_page_numbers(conn, batch))
        all_pages = set(range(1, batch["page_count"] + 1))
        missing = sorted(all_pages - served)
        if missing:
            raise ApiError(
                409,
                "sync_batch_pages_incomplete",
                (
                    "The server has not provided every page of this batch yet; "
                    "fetch all pages before confirming."
                ),
                device_id=device_id,
                sequence=sequence,
                batch_id=batch["batch_id"],
                page_count=batch["page_count"],
                served_page_numbers=sorted(served),
                missing_page_numbers=missing,
            )

        timestamp = now_iso()
        conn.execute(
            """
            UPDATE sync_batches
            SET status = 'acknowledged', acknowledged_at = ?
            WHERE batch_id = ? AND status = 'pending'
            """,
            (timestamp, batch["batch_id"]),
        )
        new_progress = max(device["last_ack_sequence"], sequence)
        conn.execute(
            "UPDATE sync_devices SET last_ack_sequence = ?, updated_at = ? WHERE id = ?",
            (new_progress, timestamp, device_id),
        )
        device = _get_device(conn, device_id)
        batch = _batch_row(conn, device_id, sequence)
        return ResponseEnvelope(
            200, _ack_body(conn, device, batch, replayed=False)  # type: ignore[arg-type]
        )


def confirm_batch_by_id(
    conn: sqlite3.Connection, batch_id: str
) -> ResponseEnvelope:
    with immediate_transaction(conn):
        batch = _batch_by_batch_id(conn, batch_id)
        if batch is None:
            raise ApiError(
                404,
                "sync_batch_not_found",
                f"Sync batch {batch_id!r} was not found.",
                batch_id=batch_id,
            )
        device_id = batch["device_id"]
        sequence = batch["sequence"]
    return confirm_batch(conn, device_id, sequence)
