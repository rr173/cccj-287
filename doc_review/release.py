"""Release tickets, immutable public versions, and retraction.

Semantics
---------
* ``prepare`` validates a whole batch and *freezes* it: the selected revision
  content, the display-order direct references, ticket-internal references
  (pinned to the same ticket's selected revision) and external references
  (pinned to the latest public version at prepare time).  Nothing an author or
  a different ticket does later can replace those locks.
* ``confirm`` publishes every item in one immediate write transaction.  If any
  locked external public version - including versions reached indirectly through
  its own references - is retracted, nothing is published and the blocking
  versions are reported.
* ``retract`` withdraws one explicit public version, never the document and
  never the author's revisions.  Readability is computed dynamically over the
  reference graph, so a retracted version also hides versions that reference it
  (directly or indirectly), while unrelated versions stay readable.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from .db import immediate_transaction
from .errors import ApiError
from .schemas import ReleaseConfirmRequest, ReleasePrepareRequest
from .service import (
    ResponseEnvelope,
    _error_envelope,
    _get_document,
    _get_revision,
    _store_envelope,
    _stored_envelope,
    new_id,
    now_iso,
    request_hash,
)


@dataclass
class _PreparedItem:
    index: int
    document_id: str
    revision: int
    title: str
    content: str
    references: list[str]


# ---------------------------------------------------------------------------
# Public-version graph helpers
# ---------------------------------------------------------------------------


def _latest_public_version(
    conn: sqlite3.Connection, document_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM public_versions
        WHERE document_id = ?
        ORDER BY release_ord DESC, rowid DESC
        LIMIT 1
        """,
        (document_id,),
    ).fetchone()


def _public_version_row(
    conn: sqlite3.Connection, public_version_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM public_versions WHERE id = ?",
        (public_version_id,),
    ).fetchone()


def _is_retracted(conn: sqlite3.Connection, public_version_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM public_retractions WHERE public_version_id = ?",
            (public_version_id,),
        ).fetchone()
        is not None
    )


def _ref_ids(conn: sqlite3.Connection, public_version_id: str) -> list[str]:
    return [
        row["target_public_version_id"]
        for row in conn.execute(
            """
            SELECT target_public_version_id
            FROM public_version_refs
            WHERE public_version_id = ?
            ORDER BY ref_index ASC
            """,
            (public_version_id,),
        ).fetchall()
    ]


def blocked_versions(
    conn: sqlite3.Connection,
    public_version_ids: Iterable[str],
) -> list[str]:
    """Return retracted versions reachable from the supplied roots (incl. roots).

    A version is unreadable when it is retracted itself or when any version it
    references directly or indirectly is retracted.  Traversal is breadth-first
    and deterministic (reference order), which keeps error bodies stable.
    """

    seen: set[str] = set()
    blocked: list[str] = []
    queue: deque[str] = deque()
    for root in public_version_ids:
        if root not in seen:
            seen.add(root)
            queue.append(root)

    while queue:
        current = queue.popleft()
        if _is_retracted(conn, current):
            blocked.append(current)
        for target in _ref_ids(conn, current):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return blocked


def _public_version_body(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    refs = conn.execute(
        """
        SELECT r.ref_index, r.target_public_version_id,
               v.document_id AS target_document_id
        FROM public_version_refs r
        LEFT JOIN public_versions v ON v.id = r.target_public_version_id
        WHERE r.public_version_id = ?
        ORDER BY r.ref_index ASC
        """,
        (row["id"],),
    ).fetchall()
    return {
        "public_version_id": row["id"],
        "document_id": row["document_id"],
        "revision": row["revision"],
        "title": row["title"],
        "content": row["content"],
        "ticket_id": row["ticket_id"],
        "released_at": row["created_at"],
        "retracted": _is_retracted(conn, row["id"]),
        "references": [
            {
                "ref_index": ref["ref_index"],
                "document_id": ref["target_document_id"],
                "public_version_id": ref["target_public_version_id"],
            }
            for ref in refs
        ],
    }


def _blocked_error(
    document_id: str | None,
    public_version_id: str,
    blocked_ids: list[str],
    **extra: Any,
) -> ApiError:
    return ApiError(
        409,
        "public_version_blocked",
        (
            "This public version is not readable because it, or a version it "
            "directly or indirectly references, has been retracted."
        ),
        document_id=document_id,
        public_version_id=public_version_id,
        blocked_public_version_ids=blocked_ids,
        **extra,
    )


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


def _find_cycles(
    edges: dict[str, list[str]],
    item_indexes: dict[str, int],
) -> list[dict[str, Any]]:
    """Find reference cycles inside a ticket, one concrete path per cycle.

    Every node that sits on a cycle belongs to a non-trivial strongly connected
    component (or has a self-loop).  Reporting one representative path per
    component keeps the response small and names an actionable cycle.
    """

    # Iterative Tarjan SCC over the ticket-internal edges.
    index_counter = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []

    for root in edges:
        if root in indexes:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_pos = work[-1]
            if child_pos == 0 and node not in indexes:
                indexes[node] = index_counter
                lowlinks[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)
            children = edges.get(node, [])
            if child_pos < len(children):
                work[-1] = (node, child_pos + 1)
                child = children[child_pos]
                if child not in indexes:
                    work.append((child, 0))
                elif child in on_stack:
                    lowlinks[node] = min(lowlinks[node], indexes[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlinks[parent] = min(lowlinks[parent], lowlinks[node])
            if lowlinks[node] == indexes[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

    cycles: list[dict[str, Any]] = []
    for component in components:
        members = sorted(component, key=lambda doc: item_indexes[doc])
        self_loop = any(
            doc in edges.get(doc, []) for doc in members
        )
        if len(members) < 2 and not self_loop:
            continue
        # Deterministic concrete path: shortest cycle through the first member.
        start = members[0]
        path = _shortest_cycle_path(edges, start, set(component))
        cycles.append(
            {
                "code": "cyclic_reference",
                "document_ids": path,
                "item_indexes": [item_indexes[doc] for doc in path[:-1]],
                "message": (
                    "Direct references form a cycle inside this ticket: "
                    + " -> ".join(path)
                ),
            }
        )

    cycles.sort(key=lambda entry: entry["item_indexes"])
    return cycles


def _shortest_cycle_path(
    edges: dict[str, list[str]],
    start: str,
    allowed: set[str],
) -> list[str]:
    """BFS from ``start`` back to ``start`` within the component."""

    parents: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for neighbour in edges.get(current, []):
            if neighbour not in allowed:
                continue
            if neighbour == start:
                # Reconstruct current -> ... -> start -> start
                chain = [current]
                while parents[chain[-1]] is not None:
                    chain.append(parents[chain[-1]])  # type: ignore[arg-type]
                chain.reverse()
                return chain + [start]
            if neighbour not in parents:
                parents[neighbour] = current
                queue.append(neighbour)
    # A self-loop was handled above; reaching here would mean no cycle exists.
    return [start, start]


def _ticket_items(
    conn: sqlite3.Connection, ticket_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM release_items
        WHERE ticket_id = ?
        ORDER BY item_index ASC
        """,
        (ticket_id,),
    ).fetchall()


def _ticket_reference_rows(
    conn: sqlite3.Connection, ticket_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM release_references
        WHERE ticket_id = ?
        ORDER BY item_index ASC, ref_index ASC
        """,
        (ticket_id,),
    ).fetchall()


def _ticket_result(conn: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
    ticket = conn.execute(
        "SELECT * FROM release_tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if ticket is None:
        raise ApiError(404, "release_ticket_not_found", f"Ticket {ticket_id!r} was not found.")

    items = _ticket_items(conn, ticket_id)
    ref_rows = _ticket_reference_rows(conn, ticket_id)
    refs_by_item: dict[int, list[dict[str, Any]]] = {}
    for ref in ref_rows:
        refs_by_item.setdefault(ref["item_index"], []).append(
            {
                "ref_index": ref["ref_index"],
                "document_id": ref["document_id"],
                "target": ref["scope"],
                "revision": ref["locked_revision"],
                "public_version_id": ref["locked_public_version_id"],
                "same_ticket": ref["scope"] == "ticket",
            }
        )

    body: dict[str, Any] = {
        "ticket_id": ticket["id"],
        "status": ticket["status"],
        "prepared_at": ticket["created_at"],
        "released_at": ticket["released_at"],
        "items": [
            {
                "item_index": item["item_index"],
                "document_id": item["document_id"],
                "revision": item["revision"],
                "title": item["title"],
                "content_length": len(item["content"]),
                "references": refs_by_item.get(item["item_index"], []),
            }
            for item in items
        ],
    }

    external_locks = [
        {
            "document_id": ref["document_id"],
            "revision": ref["locked_revision"],
            "public_version_id": ref["locked_public_version_id"],
        }
        for ref in ref_rows
        if ref["scope"] == "external"
    ]
    body["external_locks"] = external_locks

    if ticket["status"] == "released" and ticket["result_json"]:
        body["result"] = json.loads(ticket["result_json"])
    return body


def prepare_release(
    conn: sqlite3.Connection,
    payload: ReleasePrepareRequest,
) -> ResponseEnvelope:
    scope = "release-prepare"
    payload_hash = request_hash(payload.model_dump())
    # Validation, ticket creation, and idempotent replay all happen under one
    # immediate write transaction so concurrent duplicate prepares cannot create
    # two tickets for the same request_id.
    with immediate_transaction(conn):
        if payload.request_id:
            stored = _stored_envelope(conn, scope, payload.request_id, payload_hash)
            if stored is not None:
                return stored

        problems: list[dict[str, Any]] = []

        document_ids = [item.document_id for item in payload.items]
        duplicate_ids = sorted(
            {doc_id for doc_id in document_ids if document_ids.count(doc_id) > 1}
        )
        for doc_id in duplicate_ids:
            problems.append(
                {
                    "code": "duplicate_document_in_ticket",
                    "document_id": doc_id,
                    "message": "A document may appear only once per release ticket.",
                }
            )

        ticket_indexes: dict[str, int] = {}
        prepared_items: list[_PreparedItem] = []
        for item_index, item in enumerate(payload.items):
            if item.document_id in ticket_indexes:
                continue
            ticket_indexes[item.document_id] = item_index
            try:
                document = _get_document(conn, item.document_id)
            except ApiError:
                problems.append(
                    {
                        "code": "document_not_found",
                        "item_index": item_index,
                        "document_id": item.document_id,
                    }
                )
                continue
            revision = _get_revision(conn, item.document_id, item.revision)
            if revision is None:
                problems.append(
                    {
                        "code": "revision_not_found",
                        "item_index": item_index,
                        "document_id": item.document_id,
                        "revision": item.revision,
                        "current_revision": document["current_revision"],
                    }
                )
                continue
            prepared_items.append(
                _PreparedItem(
                    index=item_index,
                    document_id=item.document_id,
                    revision=item.revision,
                    title=document["title"],
                    content=revision["content"],
                    references=list(item.references),
                )
            )

        # Direct-reference cycles can be checked before looking at publication
        # state, so the error names the cycle even if other problems also exist.
        ticket_docs = {entry.document_id for entry in prepared_items}
        edges = {
            entry.document_id: [ref for ref in entry.references if ref in ticket_docs]
            for entry in prepared_items
        }
        cycles = _find_cycles(
            edges,
            {doc_id: ticket_indexes[doc_id] for doc_id in edges},
        )
        problems.extend(cycles)

        # Resolve external references to the latest public version *at prepare
        # time*.  No fallback to an older version when that latest one is
        # unreadable - that must be reported instead.
        external_targets: dict[str, sqlite3.Row | None] = {}
        for entry in prepared_items:
            for ref_index, ref_doc_id in enumerate(entry.references):
                if ref_doc_id in ticket_docs:
                    continue
                if ref_doc_id not in external_targets:
                    external_targets[ref_doc_id] = _latest_public_version(conn, ref_doc_id)
                target = external_targets[ref_doc_id]
                if target is None:
                    problems.append(
                        {
                            "code": "external_document_has_no_public_version",
                            "item_index": entry.index,
                            "ref_index": ref_index,
                            "document_id": entry.document_id,
                            "referenced_document_id": ref_doc_id,
                            "message": (
                                "The externally referenced document has never been "
                                "successfully released."
                            ),
                        }
                    )
                    continue
                blocked = blocked_versions(conn, [target["id"]])
                if blocked:
                    problems.append(
                        {
                            "code": "external_latest_public_version_blocked",
                            "item_index": entry.index,
                            "ref_index": ref_index,
                            "document_id": entry.document_id,
                            "referenced_document_id": ref_doc_id,
                            "latest_public_version_id": target["id"],
                            "blocked_public_version_ids": blocked,
                            "message": (
                                "The latest public version of the referenced "
                                "document is not readable, directly or indirectly."
                            ),
                        }
                    )

        if problems:
            envelope = _error_envelope(
                409,
                "release_prepare_rejected",
                "The release ticket was rejected; nothing was prepared.",
                problems=problems,
                cycle_count=len(cycles),
            )
            if payload.request_id:
                _store_envelope(conn, scope, payload.request_id, payload_hash, envelope)
            return envelope

        ticket_id = new_id("rel")
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO release_tickets (id, status, created_at)
            VALUES (?, 'prepared', ?)
            """,
            (ticket_id, timestamp),
        )
        item_by_doc = {entry.document_id: entry for entry in prepared_items}
        for entry in prepared_items:
            conn.execute(
                """
                INSERT INTO release_items (
                    ticket_id, item_index, document_id, revision, title, content
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    entry.index,
                    entry.document_id,
                    entry.revision,
                    entry.title,
                    entry.content,
                ),
            )
            for ref_index, ref_doc_id in enumerate(entry.references):
                if ref_doc_id in item_by_doc:
                    conn.execute(
                        """
                        INSERT INTO release_references (
                            ticket_id, item_index, ref_index, document_id,
                            scope, locked_revision, locked_public_version_id
                        ) VALUES (?, ?, ?, ?, 'ticket', ?, NULL)
                        """,
                        (
                            ticket_id,
                            entry.index,
                            ref_index,
                            ref_doc_id,
                            item_by_doc[ref_doc_id].revision,
                        ),
                    )
                else:
                    target = external_targets[ref_doc_id]
                    conn.execute(
                        """
                        INSERT INTO release_references (
                            ticket_id, item_index, ref_index, document_id,
                            scope, locked_revision, locked_public_version_id
                        ) VALUES (?, ?, ?, ?, 'external', ?, ?)
                        """,
                        (
                            ticket_id,
                            entry.index,
                            ref_index,
                            ref_doc_id,
                            target["revision"],
                            target["id"],
                        ),
                    )

        body = _ticket_result(conn, ticket_id)
        envelope = ResponseEnvelope(201, body)
        if payload.request_id:
            _store_envelope(conn, scope, payload.request_id, payload_hash, envelope)
        return envelope


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def confirm_release(
    conn: sqlite3.Connection,
    ticket_id: str,
    payload: ReleaseConfirmRequest,
) -> ResponseEnvelope:
    with immediate_transaction(conn):
        ticket = conn.execute(
            "SELECT * FROM release_tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if ticket is None:
            raise ApiError(
                404, "release_ticket_not_found", f"Ticket {ticket_id!r} was not found."
            )

        # Confirming an already released ticket is a pure replay: no new public
        # versions, no change to any document's latest public version.
        if ticket["status"] == "released":
            return ResponseEnvelope(
                200, json.loads(ticket["result_json"]), replay=True
            )

        items = _ticket_items(conn, ticket_id)
        ref_rows = _ticket_reference_rows(conn, ticket_id)
        item_document_by_index = {
            item["item_index"]: item["document_id"] for item in items
        }

        # Re-check every locked external version, including its transitive
        # closure.  Ticket-internal versions cannot be retracted before they
        # exist, but their external locks are exactly the ones checked here.
        external_refs = [ref for ref in ref_rows if ref["scope"] == "external"]
        blocked_causes: list[dict[str, Any]] = []
        for ref in external_refs:
            blocked = blocked_versions(conn, [ref["locked_public_version_id"]])
            if blocked:
                blocked_causes.append(
                    {
                        "item_index": ref["item_index"],
                        "item_document_id": item_document_by_index[ref["item_index"]],
                        "ref_index": ref["ref_index"],
                        "referenced_document_id": ref["document_id"],
                        "locked_revision": ref["locked_revision"],
                        "locked_public_version_id": ref["locked_public_version_id"],
                        "blocked_public_version_ids": blocked,
                    }
                )
        if blocked_causes:
            envelope = _error_envelope(
                409,
                "release_blocked_by_retraction",
                (
                    "A public version locked by this ticket, or a version it "
                    "references indirectly, has been retracted. The whole ticket "
                    "was not published and no earlier public entry changed."
                ),
                ticket_id=ticket_id,
                released=False,
                blocks=blocked_causes,
                blocked_public_version_ids=sorted(
                    {
                        version_id
                        for cause in blocked_causes
                        for version_id in cause["blocked_public_version_ids"]
                    }
                ),
            )
            return envelope

        # Assign a single global, monotonic order so "latest public version"
        # is unambiguous even across tickets.
        base_ord = conn.execute(
            "SELECT COALESCE(MAX(release_ord), 0) FROM public_versions"
        ).fetchone()[0]
        timestamp = now_iso()

        published: dict[int, str] = {}
        target_id_by_document: dict[str, str] = {}
        for offset, item in enumerate(items):
            public_version_id = new_id("pub")
            published[item["item_index"]] = public_version_id
            target_id_by_document[item["document_id"]] = public_version_id
            conn.execute(
                """
                INSERT INTO public_versions (
                    id, document_id, revision, ticket_id, release_ord,
                    title, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_version_id,
                    item["document_id"],
                    item["revision"],
                    ticket_id,
                    base_ord + offset + 1,
                    item["title"],
                    item["content"],
                    timestamp,
                ),
            )

        # Freeze the direct reference order.  Ticket-internal targets resolve to
        # the same ticket's public version; external targets to the lock.
        for ref in ref_rows:
            if ref["scope"] == "ticket":
                target_id = target_id_by_document[ref["document_id"]]
            else:
                target_id = ref["locked_public_version_id"]
            conn.execute(
                """
                INSERT INTO public_version_refs (
                    public_version_id, ref_index, target_public_version_id
                ) VALUES (?, ?, ?)
                """,
                (
                    published[ref["item_index"]],
                    ref["ref_index"],
                    target_id,
                ),
            )

        result = {
            "ticket_id": ticket_id,
            "status": "released",
            "released_at": timestamp,
            "items": [],
        }
        for item in items:
            refs = conn.execute(
                """
                SELECT r.ref_index, r.target_public_version_id,
                       v.document_id AS target_document_id
                FROM public_version_refs r
                LEFT JOIN public_versions v ON v.id = r.target_public_version_id
                WHERE r.public_version_id = ?
                ORDER BY r.ref_index ASC
                """,
                (published[item["item_index"]],),
            ).fetchall()
            result["items"].append(
                {
                    "item_index": item["item_index"],
                    "document_id": item["document_id"],
                    "revision": item["revision"],
                    "title": item["title"],
                    "public_version_id": published[item["item_index"]],
                    "references": [
                        {
                            "ref_index": ref["ref_index"],
                            "document_id": ref["target_document_id"],
                            "public_version_id": ref["target_public_version_id"],
                        }
                        for ref in refs
                    ],
                }
            )

        result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            UPDATE release_tickets
            SET status = 'released', result_json = ?, released_at = ?
            WHERE id = ? AND status = 'prepared'
            """,
            (result_json, timestamp, ticket_id),
        )
        return ResponseEnvelope(201, result)


# ---------------------------------------------------------------------------
# Retraction and public reads
# ---------------------------------------------------------------------------


def retract_public_version(
    conn: sqlite3.Connection,
    public_version_id: str,
    reason: str | None,
) -> dict[str, Any]:
    with immediate_transaction(conn):
        row = _public_version_row(conn, public_version_id)
        if row is None:
            raise ApiError(
                404,
                "public_version_not_found",
                f"Public version {public_version_id!r} was not found.",
            )
        # Retraction is idempotent: retracting the same explicit version twice
        # returns the stored outcome.
        existing = conn.execute(
            "SELECT * FROM public_retractions WHERE public_version_id = ?",
            (public_version_id,),
        ).fetchone()
        if existing is None:
            timestamp = now_iso()
            conn.execute(
                """
                INSERT INTO public_retractions (
                    public_version_id, reason, retracted_at
                ) VALUES (?, ?, ?)
                """,
                (public_version_id, reason, timestamp),
            )

        retraction = conn.execute(
            "SELECT * FROM public_retractions WHERE public_version_id = ?",
            (public_version_id,),
        ).fetchone()

        # Report which other public versions also stop being readable because
        # they reference this one directly or indirectly.
        affected: list[str] = []
        seen = {public_version_id}
        queue = deque([public_version_id])
        while queue:
            current = queue.popleft()
            for reverse in conn.execute(
                """
                SELECT public_version_id FROM public_version_refs
                WHERE target_public_version_id = ?
                ORDER BY public_version_id ASC
                """,
                (current,),
            ).fetchall():
                candidate = reverse["public_version_id"]
                if candidate not in seen:
                    seen.add(candidate)
                    queue.append(candidate)
                    affected.append(candidate)

        return {
            "public_version_id": public_version_id,
            "document_id": row["document_id"],
            "retracted": True,
            "retracted_at": retraction["retracted_at"],
            "reason": retraction["reason"],
            "affected_public_version_ids": affected,
        }


def read_latest_public(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    latest = _latest_public_version(conn, document_id)
    if latest is None:
        raise ApiError(
            404,
            "public_version_not_found",
            f"Document {document_id!r} has no public version.",
        )
    blocked = blocked_versions(conn, [latest["id"]])
    if blocked:
        # Deliberately no fallback to an older public version: the latest
        # public entry is the one readers asked for, and it is withdrawn.
        raise _blocked_error(document_id, latest["id"], blocked)
    return _public_version_body(conn, latest)


def read_public_version(conn: sqlite3.Connection, public_version_id: str) -> dict[str, Any]:
    row = _public_version_row(conn, public_version_id)
    if row is None:
        raise ApiError(
            404,
            "public_version_not_found",
            f"Public version {public_version_id!r} was not found.",
        )
    blocked = blocked_versions(conn, [public_version_id])
    if blocked:
        raise _blocked_error(row["document_id"], public_version_id, blocked)
    body = _public_version_body(conn, row)
    return body


def list_public_versions(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id FROM public_versions
        WHERE document_id = ?
        ORDER BY release_ord DESC, rowid DESC
        """,
        (document_id,),
    ).fetchall()
    entries = []
    for row in rows:
        blocked = blocked_versions(conn, [row["id"]])
        entries.append(
            {
                "public_version_id": row["id"],
                "readable": not blocked,
                "blocked_public_version_ids": blocked,
            }
        )
    return {"document_id": document_id, "public_versions": entries}
