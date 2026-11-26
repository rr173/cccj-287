from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from doc_review.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "inspection-test.db"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_document(client: TestClient, content: str, title: str = "manual") -> str:
    response = client.post("/documents", json={"content": content, "title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def edit(client: TestClient, doc_id: str, old: str, new: str, request_id: str) -> int:
    doc = client.get(f"/documents/{doc_id}").json()
    start = doc["content"].index(old)
    response = client.post(
        f"/documents/{doc_id}/edits",
        json={
            "base_revision": doc["current_revision"],
            "start": start,
            "end": start + len(old),
            "replacement": new,
            "request_id": request_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["revision"]


def publish(
    client: TestClient,
    items: list[dict[str, Any]],
) -> dict[str, str]:
    """Prepare + confirm a ticket; return {document_id: public_version_id}."""

    prepared = client.post("/release-tickets", json={"items": items})
    assert prepared.status_code == 201, prepared.text
    ticket_id = prepared.json()["ticket_id"]
    confirmed = client.post(f"/release-tickets/{ticket_id}/confirm", json={})
    assert confirmed.status_code == 201, confirmed.text
    return {
        item["document_id"]: item["public_version_id"]
        for item in confirmed.json()["items"]
    }


def publish_document(client: TestClient, doc_id: str, revision: int = 0) -> str:
    return publish(
        client, [{"document_id": doc_id, "revision": revision, "references": []}]
    )[doc_id]


def register(
    client: TestClient,
    asset_id: str,
    check_item: str,
    public_version_id: str,
    expected: int = 201,
    request_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "asset_id": asset_id,
        "check_item": check_item,
        "public_version_id": public_version_id,
    }
    if request_id:
        body["request_id"] = request_id
    response = client.post("/inspections", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def retract(client: TestClient, public_version_id: str, reason: str = "errata") -> Any:
    response = client.post(
        f"/public/versions/{public_version_id}/retract", json={"reason": reason}
    )
    assert response.status_code == 200, response.text
    return response.json()


def open_order(
    client: TestClient,
    asset_id: str,
    check_item: str,
    manual_document_id: str,
    expected: int = 201,
    request_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "asset_id": asset_id,
        "check_item": check_item,
        "manual_document_id": manual_document_id,
    }
    if request_id:
        body["request_id"] = request_id
    response = client.post("/review-work-orders", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def complete(
    client: TestClient,
    work_order_id: str,
    public_version_id: str | None,
    expected: int = 200,
) -> Any:
    response = client.post(
        f"/review-work-orders/{work_order_id}/complete",
        json={"public_version_id": public_version_id},
    )
    assert response.status_code == expected, response.text
    return response


def pending(client: TestClient, **params: str) -> dict[str, Any]:
    response = client.get("/inspections/pending", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_records_exact_version_and_allows_independent_duplicates(
    client: TestClient,
) -> None:
    manual = create_document(client, "manual v1")
    v1 = publish_document(client, manual)
    v2 = publish_document(client, manual)  # independent identical release

    first = register(client, "forklift-7", "brake-pad", v1)
    second = register(client, "forklift-7", "brake-pad", v2)

    assert first["record_id"] != second["record_id"]
    assert first["registered_public_version_id"] == v1
    assert second["registered_public_version_id"] == v2
    assert first["current_basis_public_version_id"] == v1
    assert first["manual_document_id"] == manual
    assert first["status"] == "current"

    listing = client.get("/inspections", params={"asset_id": "forklift-7"}).json()
    assert listing["count"] == 2


def test_register_rejects_retracted_or_indirectly_blocked_basis(
    client: TestClient,
) -> None:
    referenced = create_document(client, "referenced")
    manual = create_document(client, "manual")
    ids = publish(
        client,
        [
            {"document_id": referenced, "revision": 0, "references": []},
            {"document_id": manual, "revision": 0, "references": [referenced]},
        ],
    )
    retract(client, ids[referenced])

    refused = register(client, "pump-1", "seal", ids[manual], expected=409)
    assert refused["error"]["code"] == "inspection_basis_unreadable"
    assert ids[referenced] in refused["error"]["blocked_public_version_ids"]

    missing = register(client, "pump-1", "seal", "pub_missing", expected=404)
    assert missing["error"]["code"] == "public_version_not_found"


def test_register_is_idempotent_by_request_id(client: TestClient) -> None:
    manual = create_document(client, "m")
    version = publish_document(client, manual)
    first = register(
        client, "a", "item", version, request_id="insp-1"
    )
    repeat = client.post(
        "/inspections",
        json={
            "asset_id": "a",
            "check_item": "item",
            "public_version_id": version,
            "request_id": "insp-1",
        },
    )
    assert repeat.status_code == 201
    assert repeat.headers["idempotent-replayed"] == "true"
    assert repeat.json() == first


# ---------------------------------------------------------------------------
# Retraction -> pending
# ---------------------------------------------------------------------------


def test_retraction_marks_matching_records_pending_without_touching_others(
    client: TestClient,
) -> None:
    manual = create_document(client, "m")
    v1 = publish_document(client, manual)
    other_manual = create_document(client, "other")
    other_v1 = publish_document(client, other_manual)

    record = register(client, "asset", "check", v1)
    unrelated = register(client, "asset", "check", other_v1)

    retract(client, v1, "wrong edition")

    after = client.get(f"/inspections/{record['record_id']}").json()
    assert after["status"] == "pending_review"
    assert after["pending"] is True
    assert after["current_basis_public_version_id"] == v1
    # The registered version is historical evidence and never overwritten.
    assert after["registered_public_version_id"] == v1
    assert after["pending_round"]["retracted_public_version_ids"] == [v1]

    untouched = client.get(f"/inspections/{unrelated['record_id']}").json()
    assert untouched["status"] == "current"
    assert untouched["pending_round"] is None


def test_indirect_retraction_marks_pending(client: TestClient) -> None:
    leaf = create_document(client, "leaf")
    mid = create_document(client, "mid")
    top = create_document(client, "top")
    ids = publish(
        client,
        [
            {"document_id": leaf, "revision": 0, "references": []},
            {"document_id": mid, "revision": 0, "references": [leaf]},
            {"document_id": top, "revision": 0, "references": [mid]},
        ],
    )
    record = register(client, "asset", "check", ids[top])
    retract(client, ids[leaf])
    after = client.get(f"/inspections/{record['record_id']}").json()
    assert after["status"] == "pending_review"
    assert ids[leaf] in after["pending_round"]["retracted_public_version_ids"]


def test_second_reason_extends_round_without_new_round(client: TestClient) -> None:
    referenced = create_document(client, "ref")
    manual = create_document(client, "manual")
    ids = publish(
        client,
        [
            {"document_id": referenced, "revision": 0, "references": []},
            {"document_id": manual, "revision": 0, "references": [referenced]},
        ],
    )
    record = register(client, "asset", "check", ids[manual])

    retract(client, ids[referenced])
    first_round = client.get(f"/inspections/{record['record_id']}").json()[
        "pending_round"
    ]
    # Retracting the basis itself is a second cause for the same basis; it must
    # extend the existing round, not open a new one.
    retract(client, ids[manual])
    second = client.get(f"/inspections/{record['record_id']}").json()["pending_round"]

    assert second["round_id"] == first_round["round_id"]
    assert set(second["retracted_public_version_ids"]) == {
        ids[manual],
        ids[referenced],
    }
    pending_body = pending(client)
    assert pending_body["count"] == 1
    assert pending_body["unreadable_bases"][0]["record_id"] == record["record_id"]


def test_new_publication_does_not_close_pending_and_replaced_basis_is_noop(
    client: TestClient,
) -> None:
    manual = create_document(client, "v0")
    v1 = publish_document(client, manual)
    record = register(client, "asset", "check", v1)

    retract(client, v1)
    assert client.get(f"/inspections/{record['record_id']}").json()["status"] == (
        "pending_review"
    )

    # A fresh release of the same manual must not auto-close the record.
    edit(client, manual, "v0", "v1", "e1")
    v2 = publish_document(client, manual, revision=1)
    assert client.get(f"/inspections/{record['record_id']}").json()["status"] == (
        "pending_review"
    )

    # Close through a work order so the record's basis moves to v2.
    order = open_order(client, "asset", "check", manual)
    complete(client, order["work_order_id"], v2)
    assert client.get(f"/inspections/{record['record_id']}").json()["status"] == (
        "current"
    )

    # Retracting the old, already-replaced basis must not reopen the record.
    retract(client, v1, "late retraction")
    body = client.get(f"/inspections/{record['record_id']}").json()
    assert body["status"] == "current"
    assert body["pending_round"] is None
    assert body["current_basis_public_version_id"] == v2
    assert pending(client)["count"] == 0


# ---------------------------------------------------------------------------
# Work orders: open / complete / replay
# ---------------------------------------------------------------------------


def test_work_order_captures_scope_and_closes_into_new_basis(client: TestClient) -> None:
    manual = create_document(client, "m v0")
    v1 = publish_document(client, manual)
    record_a = register(client, "forklift-7", "brakes", v1)
    record_b = register(client, "forklift-7", "brakes", v1)
    unrelated = register(client, "forklift-9", "brakes", v1)

    retract(client, v1)
    assert pending(client)["count"] == 3

    order = open_order(client, "forklift-7", "brakes", manual)
    assert order["captured_count"] == 2
    assert {item["record_id"] for item in order["captured_records"]} == {
        record_a["record_id"],
        record_b["record_id"],
    }
    # Opening an order does not itself change pending state.
    assert pending(client)["count"] == 3

    # A new manual edition is published; the supervisor re-checks against it.
    edit(client, manual, "m v0", "m v1", "e1")
    v2 = publish_document(client, manual, revision=1)

    result = complete(client, order["work_order_id"], v2).json()
    assert result["status"] == "completed"
    assert result["closed_count"] == 2
    assert {item["record_id"] for item in result["closed_records"]} == {
        record_a["record_id"],
        record_b["record_id"],
    }
    assert result["review_public_version_id"] == v2

    for record in (record_a, record_b):
        body = client.get(f"/inspections/{record['record_id']}").json()
        assert body["status"] == "current"
        assert body["registered_public_version_id"] == v1
        assert body["current_basis_public_version_id"] == v2
        assert body["pending_round"] is None
        assert len(body["reviews"]) == 1
        assert body["reviews"][0]["public_version_id"] == v2

    # The unrelated asset is untouched.
    still_pending = client.get(f"/inspections/{unrelated['record_id']}").json()
    assert still_pending["status"] == "pending_review"


def test_work_order_without_pending_work_is_not_opened(client: TestClient) -> None:
    manual = create_document(client, "m")
    v1 = publish_document(client, manual)
    register(client, "asset", "check", v1)  # current, not pending

    refused = open_order(client, "asset", "check", manual, expected=409)
    assert refused["error"]["code"] == "no_pending_reviews"
    assert client.get("/review-work-orders").json()["count"] == 0


def test_work_order_replay_returns_first_result_and_does_not_touch_pending(
    client: TestClient,
) -> None:
    manual = create_document(client, "m v0")
    v1 = publish_document(client, manual)
    record = register(client, "asset", "check", v1)
    retract(client, v1)
    order = open_order(client, "asset", "check", manual)
    edit(client, manual, "m v0", "m v1", "e1")
    v2 = publish_document(client, manual, revision=1)

    first = complete(client, order["work_order_id"], v2)
    assert first.json()["closed_count"] == 1

    # After completion, a further retraction opens a *new* round for the record.
    retract(client, v2)
    reopened = client.get(f"/inspections/{record['record_id']}").json()
    assert reopened["status"] == "pending_review"
    new_round_id = reopened["pending_round"]["round_id"]

    replay = complete(client, order["work_order_id"], v2)
    assert replay.status_code == 200
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == first.json()
    # The old order must not close the round that opened after it completed.
    after_replay = client.get(f"/inspections/{record['record_id']}").json()
    assert after_replay["status"] == "pending_review"
    assert after_replay["pending_round"]["round_id"] == new_round_id


def test_completion_rejects_invalid_basis_and_keeps_order_open(
    client: TestClient,
) -> None:
    manual = create_document(client, "m v0")
    v1 = publish_document(client, manual)
    register(client, "asset", "check", v1)
    retract(client, v1)
    order = open_order(client, "asset", "check", manual)

    # Missing basis.
    missing = client.post(
        f"/review-work-orders/{order['work_order_id']}/complete", json={}
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "review_basis_required"

    # Wrong manual.
    other = create_document(client, "other")
    other_v = publish_document(client, other)
    wrong = complete(
        client, order["work_order_id"], other_v, expected=409
    )
    assert wrong.json()["error"]["code"] == "review_basis_wrong_manual"

    # Unreadable (retracted) basis.
    blocked = complete(client, order["work_order_id"], v1, expected=409)
    assert blocked.json()["error"]["code"] == "review_basis_unreadable"

    # Nothing was completed; order is still open and pending work remains.
    assert client.get(
        f"/review-work-orders/{order['work_order_id']}"
    ).json()["status"] == "open"
    assert pending(client)["count"] == 1


def test_completion_can_succeed_with_no_closable_records(client: TestClient) -> None:
    manual = create_document(client, "m v0")
    v1 = publish_document(client, manual)
    register(client, "asset", "check", v1)
    retract(client, v1)
    order = open_order(client, "asset", "check", manual)
    edit(client, manual, "m v0", "m v1", "e1")
    v2 = publish_document(client, manual, revision=1)

    # A different order over an overlapping record is allowed and closes first.
    overlap = open_order(client, "asset", "check", manual)
    complete(client, overlap["work_order_id"], v2)

    result = complete(client, order["work_order_id"], v2).json()
    assert result["closed_count"] == 0
    assert result["skipped_records"][0]["reason"] == "round_resolved"
    assert client.get(
        f"/review-work-orders/{order['work_order_id']}"
    ).json()["status"] == "completed"


def test_reopened_round_is_not_closable_by_old_order(client: TestClient) -> None:
    manual = create_document(client, "m0")
    v1 = publish_document(client, manual)
    record = register(client, "asset", "check", v1)
    retract(client, v1)

    old_order = open_order(client, "asset", "check", manual)
    edit(client, manual, "m0", "m1", "e1")
    v2 = publish_document(client, manual, revision=1)
    complete(client, old_order["work_order_id"], v2)

    # The record's basis is now v2.  Retract it: a brand-new round opens.
    retract(client, v2)
    reopened = client.get(f"/inspections/{record['record_id']}").json()
    assert reopened["status"] == "pending_review"
    new_round = reopened["pending_round"]["round_id"]

    edit(client, manual, "m1", "m2", "e2")
    v3 = publish_document(client, manual, revision=2)
    new_order = open_order(client, "asset", "check", manual)
    assert new_order["captured_records"][0]["round_id"] == new_round

    result = complete(client, new_order["work_order_id"], v3).json()
    assert result["closed_count"] == 1
    assert client.get(f"/inspections/{record['record_id']}").json()["status"] == (
        "current"
    )


def test_overlapping_orders_can_both_capture_same_record(client: TestClient) -> None:
    manual = create_document(client, "m0")
    v1 = publish_document(client, manual)
    record = register(client, "asset", "check", v1)
    retract(client, v1)

    first = open_order(client, "asset", "check", manual)
    second = open_order(client, "asset", "check", manual)
    assert first["captured_records"][0]["round_id"] == second["captured_records"][0][
        "round_id"
    ]

    edit(client, manual, "m0", "m1", "e1")
    v2 = publish_document(client, manual, revision=1)
    result_one = complete(client, first["work_order_id"], v2).json()
    result_two = complete(client, second["work_order_id"], v2).json()

    assert result_one["closed_count"] == 1
    # The second order captured the same round; once closed it cannot re-close.
    assert result_two["closed_count"] == 0
    body = client.get(f"/inspections/{record['record_id']}").json()
    assert body["status"] == "current"
    assert body["current_basis_public_version_id"] == v2


def test_supervisor_views_captured_and_completed_results(client: TestClient) -> None:
    manual = create_document(client, "m0")
    v1 = publish_document(client, manual)
    record = register(client, "asset", "check", v1)
    retract(client, v1)
    order = open_order(client, "asset", "check", manual)
    edit(client, manual, "m0", "m1", "e1")
    v2 = publish_document(client, manual, revision=1)
    complete(client, order["work_order_id"], v2)

    detail = client.get(f"/review-work-orders/{order['work_order_id']}").json()
    assert detail["status"] == "completed"
    assert detail["captured_records"][0]["record_id"] == record["record_id"]
    assert detail["result"]["closed_records"][0]["new_basis_public_version_id"] == v2

    listed = client.get(
        "/review-work-orders", params={"manual_document_id": manual}
    ).json()
    assert listed["count"] == 1


def test_retraction_and_open_are_serialized(tmp_path: Path) -> None:
    """Concurrent open and retraction must observe a consistent state."""

    client = TestClient(create_app(tmp_path / "race-open.db"))
    manual = create_document(client, "m")
    v1 = publish_document(client, manual)
    records = [register(client, "asset", "check", v1) for _ in range(5)]

    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def do_retract() -> Any:
        barrier.wait()
        return client.post(
            f"/public/versions/{v1}/retract", json={"reason": "race"}
        )

    def do_open() -> Any:
        barrier.wait()
        return client.post(
            "/review-work-orders",
            json={
                "asset_id": "asset",
                "check_item": "check",
                "manual_document_id": manual,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        retract_future = pool.submit(do_retract)
        open_future = pool.submit(do_open)
        retract_response = retract_future.result()
        open_response = open_future.result()

    assert retract_response.status_code == 200
    # Either the retraction committed first (order captures all 5) or the open
    # committed first (no pending yet -> 409).  Never a partial capture.
    if open_response.status_code == 201:
        assert open_response.json()["captured_count"] == 5
    else:
        assert open_response.status_code == 409
        assert open_response.json()["error"]["code"] == "no_pending_reviews"

    final_pending = pending(client)["count"]
    if open_response.status_code == 409:
        assert final_pending == 5
    for record in records:
        assert client.get(
            f"/inspections/{record['record_id']}"
        ).json()["status"] == "pending_review"


def test_open_work_order_does_not_capture_later_pending(client: TestClient) -> None:
    manual = create_document(client, "m0")
    v1 = publish_document(client, manual)
    first = register(client, "asset", "check", v1)
    retract(client, v1)

    order = open_order(client, "asset", "check", manual)
    assert order["captured_count"] == 1

    # A fresh edition becomes readable; a new inspection recorded against it is
    # current, not pending...
    edit(client, manual, "m0", "m1", "e1")
    v2 = publish_document(client, manual, revision=1)
    second = register(client, "asset", "check", v2)

    # ...until that edition is itself retracted, which opens a new pending round
    # *after* the work order was already opened.
    retract(client, v2)
    pending_body = pending(client, asset_id="asset", check_item="check")
    assert pending_body["count"] == 2

    edit(client, manual, "m1", "m2", "e2")
    v3 = publish_document(client, manual, revision=2)
    result = complete(client, order["work_order_id"], v3).json()

    # Only the record captured at open time is closed.
    assert result["closed_count"] == 1
    assert result["closed_records"][0]["record_id"] == first["record_id"]
    # The record that became pending after open keeps its own new round.
    still = client.get(f"/inspections/{second['record_id']}").json()
    assert still["status"] == "pending_review"
    assert still["pending_round"] is not None


def test_old_open_order_cannot_close_round_reopened_after_another_order(
    client: TestClient,
) -> None:
    manual = create_document(client, "m0")
    v1 = publish_document(client, manual)
    record = register(client, "asset", "check", v1)
    retract(client, v1)

    # Two orders captured the same (first) round while it was open.
    old_order = open_order(client, "asset", "check", manual)
    closer = open_order(client, "asset", "check", manual)

    edit(client, manual, "m0", "m1", "e1")
    v2 = publish_document(client, manual, revision=1)
    closed = complete(client, closer["work_order_id"], v2).json()
    assert closed["closed_count"] == 1

    # A new retraction reopens the record with a *new* round.
    retract(client, v2)
    reopened = client.get(f"/inspections/{record['record_id']}").json()
    assert reopened["status"] == "pending_review"
    new_round_id = reopened["pending_round"]["round_id"]

    edit(client, manual, "m1", "m2", "e2")
    v3 = publish_document(client, manual, revision=2)
    # Completion still requires a valid, readable basis, so give it one.
    old_result = complete(client, old_order["work_order_id"], v3).json()
    assert old_result["closed_count"] == 0
    assert old_result["skipped_records"][0]["round_id"] != new_round_id

    # The reopened round is untouched by the old order.
    still = client.get(f"/inspections/{record['record_id']}").json()
    assert still["status"] == "pending_review"
    assert still["pending_round"]["round_id"] == new_round_id


def test_state_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.db"
    first = TestClient(create_app(db_path))
    manual = create_document(first, "m0")
    v1 = publish_document(first, manual)
    record = register(first, "asset", "check", v1)
    retract(first, v1)
    order = open_order(first, "asset", "check", manual)
    edit(first, manual, "m0", "m1", "e1")
    v2 = publish_document(first, manual, revision=1)
    complete(first, order["work_order_id"], v2)

    second = TestClient(create_app(db_path))
    detail = second.get(f"/inspections/{record['record_id']}").json()
    assert detail["registered_public_version_id"] == v1
    assert detail["current_basis_public_version_id"] == v2
    assert detail["status"] == "current"
    assert len(detail["reviews"]) == 1

    order_after = second.get(f"/review-work-orders/{order['work_order_id']}").json()
    assert order_after["status"] == "completed"
    assert order_after["result"]["closed_count"] == 1

    # The original inspection evidence is still retrievable.
    assert second.get(f"/inspections/{record['record_id']}").status_code == 200
