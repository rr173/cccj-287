"""FastAPI router for offline device synchronization."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from .db import connect
from .errors import ApiError
from .schemas import DeviceRegisterRequest
from .sync import (
    coerce_document_ids,
    confirm_batch,
    confirm_batch_by_id,
    get_batch_by_id_page,
    get_batch_metadata,
    get_batch_page,
    get_device_state,
    pull_changes,
    register_device,
    replace_subscriptions,
)

router = APIRouter(tags=["offline-sync"])


def _db_path(request: Request) -> str:
    return request.app.state.database_path


def _envelope_response(result: object) -> JSONResponse:
    return JSONResponse(
        status_code=result.status_code,  # type: ignore[attr-defined]
        content=result.body,  # type: ignore[attr-defined]
        headers=(
            {"Idempotent-Replayed": "true"}
            if getattr(result, "replay", False)
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Device registration and state
# ---------------------------------------------------------------------------


@router.post("/sync/devices")
@router.post("/sync/devices/register")
@router.post("/sync/device/register")
@router.post("/sync/register")
@router.post("/api/sync/devices")
@router.post("/api/sync/devices/register")
@router.post("/api/sync/device/register")
@router.post("/api/sync/register")
@router.post("/devices")
@router.post("/devices/register")
@router.post("/device/register")
@router.post("/api/devices")
@router.post("/api/device/register")
def register_sync_device(
    request: Request, payload: DeviceRegisterRequest
) -> JSONResponse:
    conn = connect(_db_path(request))
    try:
        return _envelope_response(register_device(conn, payload))
    finally:
        conn.close()


@router.get("/sync/devices/{device_id}")
@router.get("/api/sync/devices/{device_id}")
@router.get("/devices/{device_id}")
@router.get("/api/devices/{device_id}")
def read_sync_device(request: Request, device_id: str) -> dict[str, Any]:
    conn = connect(_db_path(request))
    try:
        return get_device_state(conn, device_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subscription replacement. A raw JSON list is accepted as well as the preferred
# {"document_ids": [...]} object.
# ---------------------------------------------------------------------------


@router.put("/sync/devices/{device_id}/subscriptions")
@router.post("/sync/devices/{device_id}/subscriptions")
@router.put("/sync/devices/{device_id}/subscription")
@router.post("/sync/devices/{device_id}/subscription")
@router.post("/sync/devices/{device_id}/subscribe")
@router.put("/api/sync/devices/{device_id}/subscriptions")
@router.post("/api/sync/devices/{device_id}/subscriptions")
@router.post("/api/sync/devices/{device_id}/subscribe")
@router.put("/sync/{device_id}/subscriptions")
@router.post("/sync/{device_id}/subscriptions")
@router.post("/sync/{device_id}/subscribe")
@router.put("/devices/{device_id}/subscriptions")
@router.post("/devices/{device_id}/subscriptions")
@router.post("/devices/{device_id}/subscribe")
def update_sync_subscriptions(
    request: Request,
    device_id: str,
    payload: Any = Body(default=None),
) -> dict[str, Any]:
    document_ids = coerce_document_ids(
        payload if payload is not None else {"document_ids": []}
    )
    conn = connect(_db_path(request))
    try:
        return replace_subscriptions(conn, device_id, document_ids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pull / pages
# ---------------------------------------------------------------------------


@router.get("/sync")
@router.get("/sync/pull")
@router.get("/api/sync")
@router.get("/api/sync/pull")
def pull_device_changes_by_query(
    request: Request, device_id: str = Query(..., min_length=1)
) -> dict[str, Any]:
    conn = connect(_db_path(request))
    try:
        return pull_changes(conn, device_id)
    finally:
        conn.close()


@router.post("/sync")
@router.post("/sync/pull")
@router.post("/api/sync")
@router.post("/api/sync/pull")
def pull_device_changes_by_body_or_query(
    request: Request,
    device_id: str | None = Query(default=None),
    payload: Any = Body(default=None),
) -> dict[str, Any]:
    if not device_id and isinstance(payload, dict):
        device_id = payload.get("device_id") or payload.get("id")
    if not isinstance(device_id, str) or not device_id:
        raise ApiError(
            422,
            "device_id_required",
            "Supply device_id as a query parameter or in the JSON body.",
        )
    conn = connect(_db_path(request))
    try:
        return pull_changes(conn, device_id)
    finally:
        conn.close()


@router.post("/sync/devices/{device_id}")
@router.post("/sync/devices/{device_id}/pull")
@router.get("/sync/devices/{device_id}/pull")
@router.post("/api/sync/devices/{device_id}")
@router.post("/api/sync/devices/{device_id}/pull")
@router.get("/api/sync/devices/{device_id}/pull")
@router.post("/sync/{device_id}/pull")
@router.get("/sync/{device_id}/pull")
@router.post("/devices/{device_id}")
@router.post("/devices/{device_id}/sync")
@router.get("/devices/{device_id}/sync")
def pull_device_changes(request: Request, device_id: str) -> dict[str, Any]:
    conn = connect(_db_path(request))
    try:
        return pull_changes(conn, device_id)
    finally:
        conn.close()


@router.get("/sync/devices/{device_id}/batches/{sequence}")
@router.get("/api/sync/devices/{device_id}/batches/{sequence}")
@router.get("/sync/{device_id}/batches/{sequence}")
@router.get("/devices/{device_id}/sync/batches/{sequence}")
def read_sync_batch(
    request: Request, device_id: str, sequence: int
) -> dict[str, Any]:
    conn = connect(_db_path(request))
    try:
        return get_batch_metadata(conn, device_id, sequence)
    finally:
        conn.close()


@router.get("/sync/devices/{device_id}/batches/{sequence}/pages")
@router.get("/api/sync/devices/{device_id}/batches/{sequence}/pages")
@router.get("/sync/{device_id}/batches/{sequence}/pages")
@router.get("/devices/{device_id}/sync/batches/{sequence}/pages")
def read_sync_batch_page_by_query(
    request: Request,
    device_id: str,
    sequence: int,
) -> dict[str, Any]:
    query = request.query_params
    if "page_index" in query or "pageIndex" in query:
        raw_index = query.get("page_index", query.get("pageIndex"))
        page_number = (_as_int(raw_index, "page_index") or 0) + 1
    else:
        page_number = _as_int(
            _pick(query, "page", "page_number", "pageNumber", "page_num", "p"),
            "page",
        ) or 1
    conn = connect(_db_path(request))
    try:
        return get_batch_page(conn, device_id, sequence, page_number)
    finally:
        conn.close()


@router.get("/sync/devices/{device_id}/batches/{sequence}/pages/{page_number}")
@router.get("/api/sync/devices/{device_id}/batches/{sequence}/pages/{page_number}")
@router.get("/sync/{device_id}/batches/{sequence}/pages/{page_number}")
@router.get("/sync/devices/{device_id}/batches/{sequence}/page/{page_number}")
@router.get("/sync/{device_id}/batches/{sequence}/page/{page_number}")
@router.get("/devices/{device_id}/sync/batches/{sequence}/pages/{page_number}")
def read_sync_batch_page(
    request: Request,
    device_id: str,
    sequence: int,
    page_number: int,
) -> dict[str, Any]:
    conn = connect(_db_path(request))
    try:
        return get_batch_page(conn, device_id, sequence, page_number)
    finally:
        conn.close()


@router.get("/sync/batches/{batch_id}/pages")
@router.get("/api/sync/batches/{batch_id}/pages")
def read_global_sync_batch_page_by_query(
    request: Request, batch_id: str
) -> dict[str, Any]:
    query = request.query_params
    if "page_index" in query or "pageIndex" in query:
        raw_index = query.get("page_index", query.get("pageIndex"))
        page_number = (_as_int(raw_index, "page_index") or 0) + 1
    else:
        page_number = _as_int(
            _pick(query, "page", "page_number", "pageNumber", "page_num", "p"),
            "page",
        ) or 1
    conn = connect(_db_path(request))
    try:
        return get_batch_by_id_page(conn, batch_id, page_number)
    finally:
        conn.close()


@router.get("/sync/batches/{batch_id}/pages/{page_number}")
@router.get("/api/sync/batches/{batch_id}/pages/{page_number}")
def read_global_sync_batch_page(
    request: Request, batch_id: str, page_number: int
) -> dict[str, Any]:
    conn = connect(_db_path(request))
    try:
        return get_batch_by_id_page(conn, batch_id, page_number)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------


@router.post("/sync/devices/{device_id}/batches/{sequence}/confirm")
@router.post("/sync/devices/{device_id}/batches/{sequence}/ack")
@router.post("/sync/devices/{device_id}/batches/{sequence}/acknowledge")
@router.post("/api/sync/devices/{device_id}/batches/{sequence}/confirm")
@router.post("/api/sync/devices/{device_id}/batches/{sequence}/ack")
@router.post("/sync/{device_id}/batches/{sequence}/confirm")
@router.post("/sync/{device_id}/batches/{sequence}/ack")
@router.post("/devices/{device_id}/sync/batches/{sequence}/confirm")
@router.post("/devices/{device_id}/sync/batches/{sequence}/ack")
def confirm_device_batch(
    request: Request,
    device_id: str,
    sequence: int,
    payload: Any = Body(default=None),
) -> JSONResponse:
    del payload  # Optional body; confirmation identity is in the path.
    conn = connect(_db_path(request))
    try:
        return _envelope_response(confirm_batch(conn, device_id, sequence))
    finally:
        conn.close()


@router.post("/sync/batches/{batch_id}/confirm")
@router.post("/sync/batches/{batch_id}/ack")
@router.post("/api/sync/batches/{batch_id}/confirm")
@router.post("/api/sync/batches/{batch_id}/ack")
def confirm_global_batch(
    request: Request, batch_id: str, payload: Any = Body(default=None)
) -> JSONResponse:
    del payload
    conn = connect(_db_path(request))
    try:
        return _envelope_response(confirm_batch_by_id(conn, batch_id))
    finally:
        conn.close()


def _pick(mapping: Any, *keys: str) -> Any:
    if mapping is None or not hasattr(mapping, "get"):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _as_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ApiError(422, "invalid_integer", f"{field} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            422, "invalid_integer", f"{field} must be an integer.", received=value
        ) from exc


@router.get("/sync/page")
@router.get("/sync/pages")
@router.get("/api/sync/page")
@router.get("/api/sync/pages")
def read_flat_sync_page(request: Request) -> dict[str, Any]:
    query = request.query_params
    batch_id = _pick(query, "batch_id", "batchId")
    device_id = _pick(query, "device_id", "deviceId")
    sequence = _as_int(
        _pick(query, "sequence", "seq", "batch_sequence", "cursor"),
        "sequence",
    )
    if "page_index" in query or "pageIndex" in query:
        page_number = (
            _as_int(query.get("page_index", query.get("pageIndex")), "page_index")
            or 0
        ) + 1
    else:
        page_number = _as_int(
            _pick(query, "page", "page_number", "pageNumber", "page_num", "p"),
            "page",
        ) or 1

    conn = connect(_db_path(request))
    try:
        if batch_id:
            return get_batch_by_id_page(conn, str(batch_id), page_number)
        if device_id and sequence is not None:
            return get_batch_page(
                conn, str(device_id), sequence, page_number
            )
        raise ApiError(
            422,
            "sync_page_identity_required",
            "Supply batch_id or both device_id and sequence.",
        )
    finally:
        conn.close()


@router.post("/sync/ack")
@router.post("/sync/confirm")
@router.post("/api/sync/ack")
@router.post("/api/sync/confirm")
def confirm_flat_sync_batch(
    request: Request, payload: Any = Body(default=None)
) -> JSONResponse:
    query = request.query_params
    batch_id = _pick(payload, "batch_id", "batchId") or _pick(
        query, "batch_id", "batchId"
    )
    device_id = _pick(payload, "device_id", "deviceId") or _pick(
        query, "device_id", "deviceId"
    )
    sequence = _as_int(
        _pick(payload, "sequence", "seq", "batch_sequence")
        or _pick(query, "sequence", "seq", "batch_sequence", "cursor"),
        "sequence",
    )

    conn = connect(_db_path(request))
    try:
        if batch_id:
            return _envelope_response(confirm_batch_by_id(conn, str(batch_id)))
        if device_id and sequence is not None:
            return _envelope_response(
                confirm_batch(conn, str(device_id), sequence)
            )
        raise ApiError(
            422,
            "sync_confirmation_identity_required",
            "Supply batch_id or both device_id and sequence.",
        )
    finally:
        conn.close()
