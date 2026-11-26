"""FastAPI application and HTTP routes."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from .db import connect, init_db, read_transaction
from .errors import ApiError
from .inspection import (
    complete_work_order,
    get_inspection,
    get_work_order,
    list_inspections,
    list_pending,
    list_work_orders,
    open_work_order,
    register_inspection,
)
from .schemas import (
    AcceptRequest,
    CloseRequest,
    DocumentCreate,
    EditCreate,
    InspectionCreate,
    ReleaseConfirmRequest,
    ReleasePrepareRequest,
    RetractRequest,
    SuggestionGroupCreate,
    WorkOrderCompleteRequest,
    WorkOrderCreate,
)
from .release import (
    confirm_release,
    list_public_versions,
    prepare_release,
    read_latest_public,
    read_public_version,
    retract_public_version,
    _ticket_result as release_ticket_body,
)
from .service import (
    accept_group,
    close_group,
    create_document,
    create_suggestion_group,
    get_accept_result,
    get_document,
    get_group,
    get_revision,
    list_groups,
    list_revisions,
    submit_edit,
)
from .sync_api import router as sync_router


def create_app(database_path: str | Path = "data/review.db") -> FastAPI:
    database_target = str(database_path)
    memory_keeper = None
    if database_target == ":memory:":
        # A shared-cache URI lets request connections see one in-memory DB.
        database_target = (
            f"file:docreview_{uuid.uuid4().hex}?mode=memory&cache=shared"
        )
        memory_keeper = connect(database_target)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if memory_keeper is not None:
            memory_keeper.close()

    app = FastAPI(
        title="Document Review Backend",
        version="1.0.0",
        description=(
            "Codepoint-accurate document revisions and drift-aware suggestion groups."
        ),
        lifespan=lifespan,
    )

    app.state.database_path = database_target
    app.state.memory_keeper = memory_keeper
    app.include_router(sync_router)

    # Initialize synchronously so TestClient and `uvicorn` are immediately ready.
    init_connection = connect(app.state.database_path)
    try:
        init_db(init_connection)
    finally:
        init_connection.close()

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.to_dict()},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "document-review-backend",
            "docs": "/docs",
            "health": "/health",
            "offline_sync": {
                "register": "POST /sync/devices",
                "subscriptions": "PUT /sync/devices/{device_id}/subscriptions",
                "pull": "POST /sync/devices/{device_id}/pull",
                "page": "GET /sync/devices/{device_id}/batches/{sequence}/pages/{page}",
                "confirm": "POST /sync/devices/{device_id}/batches/{sequence}/confirm",
            },
            "inspection_review": {
                "register": "POST /inspections",
                "pending": "GET /inspections/pending",
                "open_work_order": "POST /review-work-orders",
                "complete_work_order": "POST /review-work-orders/{work_order_id}/complete",
            },
        }

    @app.post("/documents", status_code=201)
    def post_document(payload: DocumentCreate) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return create_document(conn, payload)
        finally:
            conn.close()

    @app.get("/documents/{document_id}")
    def read_document(document_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return get_document(conn, document_id)
        finally:
            conn.close()

    @app.get("/documents/{document_id}/revisions")
    def read_revisions(document_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return {
                "document_id": document_id,
                "revisions": list_revisions(conn, document_id),
            }
        finally:
            conn.close()

    @app.get("/documents/{document_id}/revisions/{revision}")
    def read_revision(document_id: str, revision: int) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return get_revision(conn, document_id, revision)
        finally:
            conn.close()

    @app.post("/documents/{document_id}/edits")
    def post_edit(document_id: str, payload: EditCreate) -> JSONResponse:
        conn = connect(app.state.database_path)
        try:
            result = submit_edit(conn, document_id, payload)
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.post("/documents/{document_id}/suggestion-groups")
    def post_group(
        document_id: str,
        payload: SuggestionGroupCreate,
        strict: bool | None = Query(
            default=None,
            description="If true, drift conflicts return 409 and no group is stored.",
        ),
    ) -> JSONResponse:
        if strict is not None:
            payload.strict = strict
        conn = connect(app.state.database_path)
        try:
            result = create_suggestion_group(conn, document_id, payload)
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.get("/documents/{document_id}/suggestion-groups")
    def get_groups(document_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return list_groups(conn, document_id)
        finally:
            conn.close()

    @app.get("/suggestion-groups/{group_id}")
    def read_group(group_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return get_group(conn, group_id)
        finally:
            conn.close()

    @app.get("/suggestion-groups/{group_id}/result")
    def read_accept_result(group_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return get_accept_result(conn, group_id)
        finally:
            conn.close()

    @app.post("/suggestion-groups/{group_id}/accept")
    def post_accept(
        group_id: str,
        payload: AcceptRequest | None = None,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key"
        ),
    ) -> JSONResponse:
        body_request_id = payload.request_id if payload is not None else None
        if body_request_id and idempotency_key and body_request_id != idempotency_key:
            raise ApiError(
                400,
                "conflicting_request_ids",
                "Body request_id and Idempotency-Key header differ.",
                body_request_id=body_request_id,
                header_request_id=idempotency_key,
            )
        request_id_value = body_request_id or idempotency_key
        if not request_id_value:
            raise ApiError(
                400,
                "request_id_required",
                "Supply request_id in the JSON body or an Idempotency-Key header.",
            )

        conn = connect(app.state.database_path)
        try:
            result = accept_group(conn, group_id, request_id_value)
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.post("/suggestion-groups/{group_id}/close")
    def post_close(
        group_id: str,
        payload: CloseRequest | None = None,
    ) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return close_group(
                conn,
                group_id,
                reason=payload.reason if payload is not None else None,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Release / public reading.  Everything under /public is read-only and
    # only exposes versions created by successful release tickets.
    # ------------------------------------------------------------------

    @app.post("/release-tickets", status_code=201)
    def post_release_ticket(payload: ReleasePrepareRequest) -> JSONResponse:
        conn = connect(app.state.database_path)
        try:
            result = prepare_release(conn, payload)
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.get("/release-tickets/{ticket_id}")
    def read_release_ticket(ticket_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return release_ticket_body(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/release-tickets/{ticket_id}/confirm")
    def post_release_confirm(
        ticket_id: str,
        payload: ReleaseConfirmRequest | None = None,
    ) -> JSONResponse:
        conn = connect(app.state.database_path)
        try:
            result = confirm_release(
                conn, ticket_id, payload or ReleaseConfirmRequest()
            )
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.get("/public/documents/{document_id}")
    def public_latest(document_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return read_latest_public(conn, document_id)
        finally:
            conn.close()

    @app.get("/public/documents/{document_id}/versions")
    def public_versions(document_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return list_public_versions(conn, document_id)
        finally:
            conn.close()

    @app.get("/public/versions/{public_version_id}")
    def public_version(public_version_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return read_public_version(conn, public_version_id)
        finally:
            conn.close()

    @app.post("/public/versions/{public_version_id}/retract")
    def public_retract(
        public_version_id: str,
        payload: RetractRequest | None = None,
    ) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            return retract_public_version(
                conn,
                public_version_id,
                reason=payload.reason if payload is not None else None,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # On-site inspection records and re-review work orders.
    # ------------------------------------------------------------------

    @app.post("/inspections", status_code=201)
    def post_inspection(payload: InspectionCreate) -> JSONResponse:
        conn = connect(app.state.database_path)
        try:
            result = register_inspection(conn, payload)
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.get("/inspections")
    def get_inspections(
        asset_id: str | None = Query(default=None),
        check_item: str | None = Query(default=None),
        manual_document_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return list_inspections(
                    conn,
                    asset_id=asset_id,
                    check_item=check_item,
                    manual_document_id=manual_document_id,
                )
        finally:
            conn.close()

    @app.get("/inspections/pending")
    def get_pending_inspections(
        asset_id: str | None = Query(default=None),
        check_item: str | None = Query(default=None),
        manual_document_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return list_pending(
                    conn,
                    asset_id=asset_id,
                    check_item=check_item,
                    manual_document_id=manual_document_id,
                )
        finally:
            conn.close()

    @app.get("/inspections/{record_id}")
    def read_inspection(record_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return get_inspection(conn, record_id)
        finally:
            conn.close()

    @app.post("/review-work-orders", status_code=201)
    def post_review_work_order(payload: WorkOrderCreate) -> JSONResponse:
        conn = connect(app.state.database_path)
        try:
            result = open_work_order(conn, payload)
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    @app.get("/review-work-orders")
    def get_review_work_orders(
        asset_id: str | None = Query(default=None),
        check_item: str | None = Query(default=None),
        manual_document_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return list_work_orders(
                    conn,
                    asset_id=asset_id,
                    check_item=check_item,
                    manual_document_id=manual_document_id,
                    status=status,
                )
        finally:
            conn.close()

    @app.get("/review-work-orders/{work_order_id}")
    def read_review_work_order(work_order_id: str) -> dict[str, Any]:
        conn = connect(app.state.database_path)
        try:
            with read_transaction(conn):
                return get_work_order(conn, work_order_id)
        finally:
            conn.close()

    @app.post("/review-work-orders/{work_order_id}/complete")
    def post_review_work_order_complete(
        work_order_id: str,
        payload: WorkOrderCompleteRequest | None = None,
    ) -> JSONResponse:
        conn = connect(app.state.database_path)
        try:
            result = complete_work_order(
                conn,
                work_order_id,
                payload or WorkOrderCompleteRequest(),
            )
            return JSONResponse(
                status_code=result.status_code,
                content=result.body,
                headers={"Idempotent-Replayed": "true"} if result.replay else None,
            )
        finally:
            conn.close()

    return app


app = create_app(os.environ.get("DOC_REVIEW_DB", "data/review.db"))


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("doc_review.main:app", host=host, port=port)
