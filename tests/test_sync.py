from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from doc_review.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(tmp_path / "sync-test.db")) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Release helpers
# ---------------------------------------------------------------------------


def create_doc(client: TestClient, content: str = "body", title: str = "doc") -> str:
    response = client.post("/documents", json={"content": content, "title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def release(
    client: TestClient,
    document_id: str,
    references: list[str] | None = None,
    revision: int = 0,
) -> str:
    ticket_response = client.post(
        "/release-tickets",
        json={
            "items": [
                {
                    "document_id": document_id,
                    "revision": revision,
                    "references": references or [],
                }
            ]
        },
    )
    assert ticket_response.status_code == 201, ticket_response.text
    ticket_id = ticket_response.json()["ticket_id"]
    confirmed = client.post(f"/release-tickets/{ticket_id}/confirm", json={})
    assert confirmed.status_code == 201, confirmed.text
    return confirmed.json()["items"][0]["public_version_id"]


def edit_and_release(
    client: TestClient, document_id: str, old: str, new: str
) -> str:
    doc = client.get(f"/documents/{document_id}").json()
    start = doc["content"].index(old)
    edit_response = client.post(
        f"/documents/{document_id}/edits",
        json={
            "base_revision": doc["current_revision"],
            "start": start,
            "end": start + len(old),
            "replacement": new,
            "request_id": f"edit-{document_id}-{new}",
        },
    )
    assert edit_response.status_code == 201, edit_response.text
    revision = edit_response.json()["revision"]
    return release(client, document_id, revision=revision)


def register_device(
    client: TestClient, limit: int = 100, device_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"max_page_items": limit}
    if device_id:
        body["device_id"] = device_id
    response = client.post("/sync/devices", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def subscribe(
    client: TestClient, device_id: str, document_ids: list[str]
) -> dict[str, Any]:
    response = client.put(
        f"/sync/devices/{device_id}/subscriptions",
        json={"document_ids": document_ids},
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_page(
    client: TestClient, device_id: str, sequence: int, page_number: int
) -> dict[str, Any]:
    response = client.get(
        f"/sync/devices/{device_id}/batches/{sequence}/pages/{page_number}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def fetch_all_pages(
    client: TestClient, first_page: dict[str, Any]
) -> list[dict[str, Any]]:
    pages = [first_page]
    device_id = first_page["device_id"]
    sequence = first_page["sequence"]
    for page_number in range(2, first_page["page_count"] + 1):
        pages.append(get_page(client, device_id, sequence, page_number))
    return pages


def confirm(
    client: TestClient,
    device_id: str,
    sequence: int,
    expected: int = 200,
) -> dict[str, Any]:
    response = client.post(
        f"/sync/devices/{device_id}/batches/{sequence}/confirm", json={}
    )
    assert response.status_code == expected, response.text
    return response.json()


class LocalReplica:
    """Minimal offline document store used to prove protocol convergence."""

    def __init__(self) -> None:
        self.mapping: dict[str, str | None] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.applied_sequences: list[int] = []

    def apply_pages(self, pages: list[dict[str, Any]]) -> int:
        first = pages[0]
        sequence = first["sequence"]
        # A real client collects every page before this atomic replacement.
        assert all(page["sequence"] == sequence for page in pages)
        assert {page["page_number"] for page in pages} == set(
            range(1, first["page_count"] + 1)
        )

        new_mapping = {
            item["document_id"]: item["public_version_id"]
            for item in first["subscription_mapping"]
        }
        next_versions = dict(self.versions)
        for page in pages:
            for write in page["writes"]:
                next_versions[write["public_version_id"]] = write
            for deleted_id in page["delete_ids"]:
                next_versions.pop(deleted_id, None)

        self.mapping = new_mapping
        self.versions = next_versions
        self.applied_sequences.append(sequence)
        return sequence

    def apply_pull(self, client: TestClient, pull: dict[str, Any]) -> int:
        return self.apply_pages(fetch_all_pages(client, pull))

    def target_snapshot(self) -> tuple[dict[str, str | None], set[str]]:
        return dict(self.mapping), set(self.versions)


def full_sync(
    client: TestClient,
    device_id: str,
    replica: LocalReplica | None = None,
) -> dict[str, Any]:
    pull = client.post(f"/sync/devices/{device_id}/pull").json()
    if pull["status"] == "in_sync":
        return pull
    pages = fetch_all_pages(client, pull)
    if replica is not None:
        replica.apply_pages(pages)
    confirm(client, device_id, pull["sequence"])
    return pull


# ---------------------------------------------------------------------------
# Registration and basic target graph
# ---------------------------------------------------------------------------


def test_device_registration_validates_page_limit_and_is_independent(
    client: TestClient,
) -> None:
    missing = client.post("/sync/devices", json={})
    assert missing.status_code == 201, missing.text
    assert missing.json()["max_page_items"] == 100

    for bad_limit in (0, 101, -1):
        response = client.post("/sync/devices", json={"page_size": bad_limit})
        assert response.status_code == 422, (bad_limit, response.text)
        assert response.json()["error"]["code"] == "invalid_page_limit"

    supplied = client.post(
        "/sync/devices", json={"device_id": "kitchen-tablet", "page_limit": 2}
    )
    assert supplied.status_code == 201, supplied.text
    assert supplied.json()["device_id"] == "kitchen-tablet"

    replay = client.post(
        "/sync/devices", json={"device_id": "kitchen-tablet", "page_limit": 2}
    )
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replayed") == "true"

    conflict = client.post(
        "/sync/devices", json={"device_id": "kitchen-tablet", "page_limit": 3}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "sync_device_already_registered"

    first = register_device(client, limit=1)
    second = register_device(client, limit=100)
    assert first["device_id"] != second["device_id"]
    assert first["last_ack_sequence"] == 0

    unknown = client.post("/sync/devices/dev_missing/pull")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "sync_device_not_found"


def test_first_sync_starts_empty_and_gets_root_plus_fixed_reference_closure(
    client: TestClient,
) -> None:
    leaf = create_doc(client, "leaf body", "Leaf")
    middle = create_doc(client, "middle body", "Middle")
    root = create_doc(client, "root body", "Root")

    leaf_pub = release(client, leaf)
    middle_pub = release(client, middle, references=[leaf])
    root_pub = release(client, root, references=[middle])

    device = register_device(client, limit=100)
    subscribe(client, device["device_id"], [root])

    pull = client.post(f"/sync/devices/{device['device_id']}/pull")
    assert pull.status_code == 200, pull.text
    body = pull.json()
    assert body["status"] == "batch"
    assert body["sequence"] == 1
    assert body["page_count"] == 1
    assert body["synced"] is False

    mapping = {
        item["document_id"]: item["public_version_id"]
        for item in body["subscription_mapping"]
    }
    assert mapping == {root: root_pub}

    writes = body["writes"]
    assert {write["public_version_id"] for write in writes} == {
        root_pub,
        middle_pub,
        leaf_pub,
    }
    by_id = {write["public_version_id"]: write for write in writes}
    assert by_id[root_pub]["content"] == "root body"
    assert by_id[root_pub]["references"] == [middle_pub]
    assert by_id[middle_pub]["references"] == [leaf_pub]
    assert by_id[leaf_pub]["references"] == []
    assert body["delete_ids"] == []

    replica = LocalReplica()
    replica.apply_pull(client, body)
    confirm(client, device["device_id"], 1)

    next_pull = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert next_pull["status"] == "in_sync"
    assert next_pull["synced"] is True
    assert replica.versions.keys() == {root_pub, middle_pub, leaf_pub}


def test_referenced_old_version_is_not_replaced_by_new_release(
    client: TestClient,
) -> None:
    ref_doc = create_doc(client, "ref v1")
    root_doc = create_doc(client, "root v1")
    ref_v1 = release(client, ref_doc)
    root_v1 = release(client, root_doc, references=[ref_doc])

    # The root remains frozen to ref_v1 even though ref_doc gets a new latest.
    ref_v2 = edit_and_release(client, ref_doc, "ref v1", "ref v2")

    device = register_device(client)
    subscribe(client, device["device_id"], [root_doc])
    pull = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    ids = {write["public_version_id"] for write in pull["writes"]}
    assert ids == {root_v1, ref_v1}
    assert ref_v2 not in ids
    root_write = next(w for w in pull["writes"] if w["id"] == root_v1)
    assert root_write["references"] == [ref_v1]


def test_missing_or_unreadable_subscription_is_skipped_without_fallback(
    client: TestClient,
) -> None:
    doc = create_doc(client, "version one")
    v1 = release(client, doc)
    v2 = edit_and_release(client, doc, "version one", "version two")

    device = register_device(client)
    subscribe(client, device["device_id"], [doc, "doc_does_not_exist"])

    first = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    mapping = {
        item["document_id"]: item["public_version_id"]
        for item in first["subscription_mapping"]
    }
    assert mapping[doc] == v2
    assert mapping["doc_does_not_exist"] is None
    assert {w["id"] for w in first["writes"]} == {v2}

    replica = LocalReplica()
    replica.apply_pull(client, first)
    confirm(client, device["device_id"], first["sequence"])

    # Retract the current latest. Do not select v1 as a fallback root.
    retract = client.post(f"/public/versions/{v2}/retract", json={"reason": "x"})
    assert retract.status_code == 200, retract.text

    stale_old = client.get(
        f"/sync/devices/{device['device_id']}/batches/{first['sequence']}/pages/1"
    )
    assert stale_old.status_code == 409
    assert stale_old.json()["error"]["code"] == "sync_batch_stale"

    second = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert second["sequence"] == 2
    mapping = {
        item["document_id"]: item["public_version_id"]
        for item in second["subscription_mapping"]
    }
    assert mapping[doc] is None
    assert second["target_version_ids"] == []
    assert second["writes"] == []
    assert second["delete_ids"] == [v2]
    assert v1 not in second["target_version_ids"]

    replica.apply_pull(client, second)
    assert v2 not in replica.versions
    # v1 was not part of the acknowledged v2 target/replica.
    assert v1 not in replica.versions


# ---------------------------------------------------------------------------
# Pagination, replay, and confirmation
# ---------------------------------------------------------------------------


def test_pagination_cap_replay_and_all_pages_required_for_confirm(
    client: TestClient,
) -> None:
    leaf = create_doc(client, "leaf")
    middle = create_doc(client, "middle")
    root = create_doc(client, "root")
    leaf_pub = release(client, leaf)
    middle_pub = release(client, middle, references=[leaf])
    root_pub = release(client, root, references=[middle])

    device = register_device(client, limit=2)
    subscribe(client, device["device_id"], [root])

    first_response = client.post(f"/sync/devices/{device['device_id']}/pull")
    assert first_response.status_code == 200
    first = first_response.json()
    assert first["page_count"] == 2
    assert first["page_item_count"] <= 2
    assert len(first["writes"]) + len(first["delete_ids"]) == 2

    # Retrying pull returns the same sequence/batch and same first page.
    retry = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert retry["sequence"] == first["sequence"]
    assert retry["batch_id"] == first["batch_id"]
    assert retry["writes"] == first["writes"]

    page_one_repeat = get_page(client, device["device_id"], 1, 1)
    assert page_one_repeat["writes"] == first["writes"]
    assert page_one_repeat["delete_ids"] == first["delete_ids"]

    early_confirm = client.post(
        f"/sync/devices/{device['device_id']}/batches/1/confirm", json={}
    )
    assert early_confirm.status_code == 409
    error = early_confirm.json()["error"]
    assert error["code"] == "sync_batch_pages_incomplete"
    assert error["missing_page_numbers"] == [2]

    page_two = get_page(client, device["device_id"], 1, 2)
    assert page_two["page_item_count"] == 1
    all_ids = {
        write["id"]
        for page in [first, page_two]
        for write in page["writes"]
    }
    assert all_ids == {root_pub, middle_pub, leaf_pub}

    confirmed = confirm(client, device["device_id"], 1)
    assert confirmed["last_ack_sequence"] == 1

    confirm_retry = client.post(
        f"/sync/devices/{device['device_id']}/batches/1/confirm", json={}
    )
    assert confirm_retry.status_code == 200
    assert confirm_retry.headers.get("Idempotent-Replayed") == "true"
    assert confirm_retry.json()["last_ack_sequence"] == 1


def test_invalid_page_limit_boundaries_are_accepted(client: TestClient) -> None:
    low = register_device(client, limit=1)
    high = register_device(client, limit=100)
    assert low["max_page_items"] == 1
    assert high["max_page_items"] == 100


# ---------------------------------------------------------------------------
# Subscription changes and garbage collection/reference retention
# ---------------------------------------------------------------------------


def test_subscription_change_is_independent_and_old_referenced_version_kept(
    client: TestClient,
) -> None:
    ref_doc = create_doc(client, "ref v1")
    manual_doc = create_doc(client, "manual v1")
    ref_v1 = release(client, ref_doc)
    manual_v1 = release(client, manual_doc, references=[ref_doc])
    ref_v2 = edit_and_release(client, ref_doc, "ref v1", "ref v2")

    device_a = register_device(client)
    device_b = register_device(client)
    subscribe(client, device_a["device_id"], [manual_doc, ref_doc])
    subscribe(client, device_b["device_id"], [ref_doc])

    pull_a = full_sync(client, device_a["device_id"], LocalReplica())
    assert {w["id"] for w in pull_a["writes"]} == {
        manual_v1,
        ref_v1,
        ref_v2,
    }
    pull_b = full_sync(client, device_b["device_id"], LocalReplica())
    assert {w["id"] for w in pull_b["writes"]} == {ref_v2}

    # Replace A's subscriptions: no longer subscribe to ref_doc directly.
    # manual_v1 still fixes ref_v1, while ref_v2 is no longer needed.
    subscribe(client, device_a["device_id"], [manual_doc])
    next_pull = client.post(f"/sync/devices/{device_a['device_id']}/pull").json()
    assert next_pull["sequence"] == 2
    assert next_pull["subscription_mapping"] == [
        {"document_id": manual_doc, "public_version_id": manual_v1, "readable": True, "included": True}
    ]
    assert next_pull["writes"] == []
    assert next_pull["delete_ids"] == [ref_v2]

    replica = LocalReplica()
    replica.mapping = {manual_doc: manual_v1, ref_doc: ref_v2}
    replica.versions = {
        manual_v1: {"id": manual_v1},
        ref_v1: {"id": ref_v1},
        ref_v2: {"id": ref_v2},
    }
    replica.apply_pull(client, next_pull)
    assert set(replica.versions) == {manual_v1, ref_v1}

    # Device B subscriptions were untouched.
    state_b = client.get(f"/sync/devices/{device_b['device_id']}").json()
    assert state_b["document_ids"] == [ref_doc]


def test_new_release_batch_omits_guaranteed_version_and_deletes_removed_one(
    client: TestClient,
) -> None:
    doc_a = create_doc(client, "a1")
    doc_b = create_doc(client, "b1")
    a1 = release(client, doc_a)
    b1 = release(client, doc_b)

    device = register_device(client)
    subscribe(client, device["device_id"], [doc_a, doc_b])
    first = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    confirm(client, device["device_id"], first["sequence"])

    a2 = edit_and_release(client, doc_a, "a1", "a2")
    subscribe(client, device["device_id"], [doc_a])  # remove B

    second = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert second["sequence"] == 2
    write_ids = {w["id"] for w in second["writes"]}
    assert write_ids == {a2}  # a1 and b1 are not guaranteed/current.
    assert set(second["delete_ids"]) == {a1, b1}

    # If a1 were still referenced by retained content, it would not be deleted;
    # covered separately by reference retention test. Here plain replacement.
    replica = LocalReplica()
    replica.versions = {a1: {}, b1: {}}
    replica.mapping = {doc_a: a1, doc_b: b1}
    replica.apply_pull(client, second)
    assert set(replica.versions) == {a2}
    assert replica.mapping == {doc_a: a2}


# ---------------------------------------------------------------------------
# Invalidation and reconciliation across missed/failed confirmations
# ---------------------------------------------------------------------------


def test_stale_unconfirmed_batch_refuses_pages_and_confirmation(
    client: TestClient,
) -> None:
    doc = create_doc(client, "v1")
    v1 = release(client, doc)
    device = register_device(client, limit=1)
    subscribe(client, device["device_id"], [doc])

    first = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert first["sequence"] == 1
    assert first["writes"][0]["id"] == v1

    v2 = edit_and_release(client, doc, "v1", "v2")

    old_page = client.get(f"/sync/devices/{device['device_id']}/batches/1/pages/1")
    assert old_page.status_code == 409
    assert old_page.json()["error"]["code"] == "sync_batch_stale"

    old_confirm = client.post(
        f"/sync/devices/{device['device_id']}/batches/1/confirm", json={}
    )
    assert old_confirm.status_code == 409
    assert old_confirm.json()["error"]["code"] == "sync_batch_stale"
    assert old_confirm.json()["error"]["can_revive"] is False

    new_pull = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert new_pull["sequence"] == 2
    assert new_pull["batch_id"] != first["batch_id"]
    # Empty and v1 are both possible; v2 must be sent and v1 deleted.
    new_pages = fetch_all_pages(client, new_pull)
    new_write_ids = {
        write["id"] for page in new_pages for write in page["writes"]
    }
    new_delete_ids = {
        deleted for page in new_pages for deleted in page["delete_ids"]
    }
    assert new_write_ids == {v2}
    assert new_delete_ids == {v1}

    # Pulling again does not create a third batch while target is unchanged.
    repeat = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert repeat["sequence"] == 2
    assert repeat["batch_id"] == new_pull["batch_id"]


def test_new_batch_corrects_never_applied_and_applied_with_lost_ack(
    client: TestClient,
) -> None:
    doc = create_doc(client, "one")
    v1 = release(client, doc)
    device = register_device(client, limit=100)
    subscribe(client, device["device_id"], [doc])

    batch1 = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert {w["id"] for w in batch1["writes"]} == {v1}

    # One possible device never applies batch1; another applies it but its ack
    # is lost, so neither confirms batch1 with the server.
    applied_replica = LocalReplica()
    applied_replica.apply_pull(client, batch1)
    never_applied_replica = LocalReplica()

    v2 = edit_and_release(client, doc, "one", "two")
    batch2 = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert batch2["sequence"] == 2
    assert {w["id"] for w in batch2["writes"]} == {v2}
    assert batch2["delete_ids"] == [v1]

    for replica in (applied_replica, never_applied_replica):
        replica.apply_pull(client, batch2)
        assert set(replica.versions) == {v2}
        assert replica.mapping == {doc: v2}


def test_multiple_missed_acks_use_union_and_intersection(
    client: TestClient,
) -> None:
    d1 = create_doc(client, "d1-v1")
    d2 = create_doc(client, "d2-v1")
    d3 = create_doc(client, "d3-v1")
    a = release(client, d1)
    b = release(client, d2)

    device = register_device(client)
    subscribe(client, device["device_id"], [d1, d2])
    batch1 = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert {w["id"] for w in batch1["writes"]} == {a, b}

    c = release(client, d3)
    subscribe(client, device["device_id"], [d1, d3])
    batch2 = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    # Server still has ack=0: resend current bodies and delete b.
    assert {w["id"] for w in batch2["writes"]} == {a, c}
    assert batch2["delete_ids"] == [b]

    a2 = edit_and_release(client, d1, "d1-v1", "d1-v2")
    # The device could have pulled this target, so a2 is a possible installed
    # version even if that batch and its acknowledgement were both lost.
    batch3 = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert batch3["sequence"] == 3
    assert {w["id"] for w in batch3["writes"]} == {a2, c}

    subscribe(client, device["device_id"], [d2, d3])
    batch4 = client.post(f"/sync/devices/{device['device_id']}/pull").json()

    # Possible states: empty, {a,b}, {a,c}, and {a2,c}. Current is {b,c}.
    # Nothing target is guaranteed from the empty state, so b and c are sent.
    # Every possible old ID not in target is deleted.
    assert batch4["sequence"] == 4
    assert {w["id"] for w in batch4["writes"]} == {b, c}
    assert set(batch4["delete_ids"]) == {a, a2}

    replica = LocalReplica()
    # Worst possible client applied batch3: has a2,c.
    replica.versions = {a2: {}, c: {}}
    replica.mapping = {d1: a2, d3: c}
    replica.apply_pull(client, batch4)
    assert set(replica.versions) == {b, c}
    assert replica.mapping == {d2: b, d3: c}


def test_received_ack_retry_succeeds_after_later_target_change(
    client: TestClient,
) -> None:
    doc = create_doc(client, "v1")
    v1 = release(client, doc)
    device = register_device(client)
    subscribe(client, device["device_id"], [doc])
    first = full_sync(client, device["device_id"], LocalReplica())
    confirm_retry_before_change = client.post(
        f"/sync/devices/{device['device_id']}/batches/{first['sequence']}/confirm"
    )
    assert confirm_retry_before_change.status_code == 200

    v2 = edit_and_release(client, doc, "v1", "v2")
    # The original confirmation response was lost; retry after publishing v2.
    late_retry = client.post(
        f"/sync/devices/{device['device_id']}/batches/1/confirm", json={}
    )
    assert late_retry.status_code == 200, late_retry.text
    assert late_retry.headers.get("Idempotent-Replayed") == "true"
    assert late_retry.json()["last_ack_sequence"] == 1

    pull = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert pull["sequence"] == 2
    assert {w["id"] for w in pull["writes"]} == {v2}
    assert pull["delete_ids"] == [v1]


def test_retraction_after_rollback_boundary_keeps_consistent_delete_batch(
    client: TestClient,
) -> None:
    ref = create_doc(client, "ref")
    manual = create_doc(client, "manual")
    ref_pub = release(client, ref)
    manual_pub = release(client, manual, references=[ref])

    device = register_device(client, limit=1)
    subscribe(client, device["device_id"], [manual])
    first = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert first["page_count"] == 2
    for page_number in range(2, first["page_count"] + 1):
        get_page(client, device["device_id"], 1, page_number)
    confirm(client, device["device_id"], 1)

    client.post(f"/public/versions/{ref_pub}/retract", json={"reason": "bad"})
    pull = client.post(f"/sync/devices/{device['device_id']}/pull").json()
    assert pull["status"] == "batch"
    # The reconciliation itself is paginated by the same one-item cap.
    pages = fetch_all_pages(client, pull)
    deleted_ids = {
        deleted
        for page in pages
        for deleted in page["delete_ids"]
    }
    # The referencing latest is blocked; skip the root rather than roll back.
    assert pull["subscription_mapping"][0]["public_version_id"] is None
    assert pull["target_version_ids"] == []
    assert all(page["writes"] == [] for page in pages)
    assert deleted_ids == {manual_pub, ref_pub}


def test_common_path_and_pagination_aliases_work(client: TestClient) -> None:
    doc = create_doc(client, "alias body")
    pub = release(client, doc)
    device = client.post(
        "/sync/device/register", json={"page_size": 1}
    ).json()
    device_id = device["device_id"]

    subscribed = client.post(f"/sync/{device_id}/subscribe", json=[doc])
    assert subscribed.status_code == 200, subscribed.text

    pull = client.post("/sync/pull", params={"device_id": device_id})
    assert pull.status_code == 200, pull.text
    first = pull.json()
    assert first["sequence"] == 1

    by_zero_based_query = client.get(
        f"/sync/devices/{device_id}/batches/1/pages?page_index=0"
    )
    assert by_zero_based_query.status_code == 200, by_zero_based_query.text
    assert by_zero_based_query.json()["page_number"] == 1
    assert by_zero_based_query.json()["writes"][0]["id"] == pub

    ack = client.post(
        "/sync/ack", json={"device_id": device_id, "sequence": 1}
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["last_ack_sequence"] == 1


# ---------------------------------------------------------------------------
# Persistence/restart
# ---------------------------------------------------------------------------


def test_batches_pages_subscriptions_and_ack_survive_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restart-sync.db"

    with TestClient(create_app(db_path)) as client:
        document_id = create_doc(client, "persisted")
        pub = release(client, document_id)
        registered = register_device(client, limit=1)
        device_id = registered["device_id"]
        subscribe(client, device_id, [document_id])
        first = client.post(f"/sync/devices/{device_id}/pull").json()
        assert first["page_count"] == 1

    with TestClient(create_app(db_path)) as restarted:
        state = restarted.get(f"/sync/devices/{device_id}").json()
        assert state["document_ids"] == [document_id]
        repeated = restarted.post(f"/sync/devices/{device_id}/pull").json()
        assert repeated["batch_id"] == first["batch_id"]
        assert repeated["writes"][0]["id"] == pub

        confirmed = restarted.post(
            f"/sync/devices/{device_id}/batches/1/confirm", json={}
        )
        assert confirmed.status_code == 200, confirmed.text
        again = restarted.post(
            f"/sync/devices/{device_id}/batches/1/confirm", json={}
        )
        assert again.status_code == 200
        in_sync = restarted.post(f"/sync/devices/{device_id}/pull").json()
        assert in_sync["status"] == "in_sync"
