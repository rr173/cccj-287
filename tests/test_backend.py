from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from doc_review.main import create_app
from doc_review.mapping import Replacement, map_range_through_revision


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path / "test-review.db")
    return TestClient(app)


def create_document(client: TestClient, content: str) -> str:
    response = client.post("/documents", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_boundary_affinity_rules() -> None:
    start, end = 4, 8

    # Insertion at the start is before the original range: both points shift.
    new_range = map_range_through_revision(
        start, end, [Replacement(4, 4, "ABC")], revision=1, revision_source="edit"
    )
    assert new_range[:2] == (7, 11)
    assert new_range[2] == []

    # Insertion at the end is after the range: end stays at the insertion point.
    new_range = map_range_through_revision(
        start, end, [Replacement(8, 8, "Z")], revision=1, revision_source="edit"
    )
    assert new_range[:2] == (4, 8)
    assert new_range[2] == []

    # An operation ending exactly at start is before the half-open range.
    new_range = map_range_through_revision(
        start, end, [Replacement(0, 4, "AB")], revision=1, revision_source="edit"
    )
    assert new_range[:2] == (2, 6)
    assert new_range[2] == []

    # Internal insertion conflicts.
    _, _, conflicts = map_range_through_revision(
        start, end, [Replacement(5, 5, "X")], revision=1, revision_source="edit"
    )
    assert [item["code"] for item in conflicts] == ["internal_insertion"]

    # An intersecting replacement conflicts.
    _, _, conflicts = map_range_through_revision(
        start, end, [Replacement(7, 9, "XY")], revision=1, revision_source="edit"
    )
    assert [item["code"] for item in conflicts] == ["intersecting_edit"]


def test_codepoint_prefix_edits_and_accept(client: TestClient) -> None:
    content = "前言：目标αβγ结尾"
    target = "目标αβγ"
    start = content.index(target)
    end = start + len(target)  # len() counts Unicode code points

    doc_id = create_document(client, content)
    group = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [
                {"start": start, "end": end, "replacement": "新值🙂"}
            ],
        },
    ).json()

    current = client.get(f"/documents/{doc_id}").json()
    client.post(
        f"/documents/{doc_id}/edits",
        json={
            "base_revision": current["current_revision"],
            "start": 0,
            "end": 0,
            "replacement": "[前缀一]",
            "request_id": "e1",
        },
    )
    current = client.get(f"/documents/{doc_id}").json()
    old_prefix = "前言"
    prefix_start = current["content"].index(old_prefix)
    response = client.post(
        f"/documents/{doc_id}/edits",
        json={
            "base_revision": current["current_revision"],
            "start": prefix_start,
            "end": prefix_start + len(old_prefix),
            "replacement": "引言说明",
            "request_id": "e2",
        },
    )
    assert response.status_code == 201

    located = client.get(f"/suggestion-groups/{group['id']}").json()
    item = located["items"][0]
    assert located["drift_clean"] is True
    assert item["current_text"] == target

    accepted = client.post(
        f"/suggestion-groups/{group['id']}/accept",
        json={"request_id": "accept-unicode"},
    )
    assert accepted.status_code == 200, accepted.text
    expected = "[前缀一]引言说明：新值🙂结尾"
    assert accepted.json()["content"] == expected


def test_reverted_text_still_conflicts(client: TestClient) -> None:
    doc_id = create_document(client, "0123456789")
    group = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [{"start": 2, "end": 5, "replacement": "OK"}],
        },
    ).json()

    first = client.post(
        f"/documents/{doc_id}/edits",
        json={"base_revision": 0, "start": 2, "end": 5, "replacement": "XYZ"},
    )
    assert first.status_code == 201

    second = client.post(
        f"/documents/{doc_id}/edits",
        json={"base_revision": 1, "start": 2, "end": 5, "replacement": "234"},
    )
    assert second.status_code == 201
    assert second.json()["content"] == "0123456789"

    response = client.post(
        f"/suggestion-groups/{group['id']}/accept",
        json={"request_id": "accept-revert"},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "suggestion_conflicts"
    item = error["items"][0]
    assert item["conflict"] is True
    assert item["current_text"] == "234"
    assert [conflict["revision"] for conflict in item["conflicts"]] == [1, 2]

    doc = client.get(f"/documents/{doc_id}").json()
    assert doc["current_revision"] == 2


def test_stale_edit_conflict_includes_rebased_range(client: TestClient) -> None:
    doc_id = create_document(client, "abcdef")
    first = client.post(
        f"/documents/{doc_id}/edits",
        json={
            "base_revision": 0,
            "start": 0,
            "end": 0,
            "replacement": "XY",
            "request_id": "edit-1",
        },
    )
    assert first.status_code == 201

    stale = client.post(
        f"/documents/{doc_id}/edits",
        json={
            "base_revision": 0,
            "start": 2,
            "end": 3,
            "replacement": "Z",
            "request_id": "stale-1",
        },
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "revision_conflict"
    assert error["current_revision"] == 1
    assert error["rebased_range"]["current_range"] == {"start": 4, "end": 5}

    retry = client.post(
        f"/documents/{doc_id}/edits",
        json={
            "base_revision": 0,
            "start": 2,
            "end": 3,
            "replacement": "Z",
            "request_id": "stale-1",
        },
    )
    assert retry.status_code == 409
    assert retry.headers["idempotent-replayed"] == "true"
    assert retry.json() == stale.json()


def test_overlapping_and_empty_suggestions_rejected(client: TestClient) -> None:
    doc_id = create_document(client, "abcdefgh")
    response = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [
                {"start": 1, "end": 4, "replacement": "X"},
                {"start": 3, "end": 5, "replacement": "Y"},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_suggestion_ranges"

    empty = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [{"start": 2, "end": 2, "replacement": "X"}],
        },
    )
    assert empty.status_code == 422


def test_adjacent_ranges_and_accept_idempotency(client: TestClient) -> None:
    doc_id = create_document(client, "abcdef")
    group = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [
                {"start": 0, "end": 2, "replacement": "AB"},
                {"start": 2, "end": 4, "replacement": "CD"},
            ],
        },
    )
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]

    first = client.post(
        f"/suggestion-groups/{group_id}/accept", json={"request_id": "same"}
    )
    second = client.post(
        f"/suggestion-groups/{group_id}/accept", json={"request_id": "same"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["idempotent-replayed"] == "true"
    assert first.json() == second.json()
    assert first.json()["content"] == "ABCDef"

    different = client.post(
        f"/suggestion-groups/{group_id}/accept", json={"request_id": "different"}
    )
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "suggestion_group_already_accepted"


def test_accepted_revision_participates_in_later_mapping(client: TestClient) -> None:
    doc_id = create_document(client, "one two three")
    one_start = 0
    two_start = 4
    group_one = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [
                {"start": one_start, "end": 3, "replacement": "LONG-ONE"}
            ],
        },
    ).json()
    group_two = client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [
                {"start": two_start, "end": 7, "replacement": "TWO-NEW"}
            ],
        },
    ).json()

    accepted = client.post(
        f"/suggestion-groups/{group_one['id']}/accept",
        json={"request_id": "one"},
    )
    assert accepted.status_code == 200

    mapped = client.get(f"/suggestion-groups/{group_two['id']}").json()
    assert mapped["drift_clean"] is True
    assert mapped["items"][0]["current_text"] == "two"

    accepted_two = client.post(
        f"/suggestion-groups/{group_two['id']}/accept",
        json={"request_id": "two"},
    )
    assert accepted_two.status_code == 200
    assert accepted_two.json()["content"] == "LONG-ONE TWO-NEW three"


def test_persistence_between_app_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "persist.db"
    first_client = TestClient(create_app(db_path))
    doc_id = create_document(first_client, "persistent text")
    group = first_client.post(
        f"/documents/{doc_id}/suggestion-groups",
        json={
            "base_revision": 0,
            "suggestions": [{"start": 0, "end": 10, "replacement": "durable"}],
        },
    ).json()
    accepted = first_client.post(
        f"/suggestion-groups/{group['id']}/accept", json={"request_id": "p1"}
    )
    assert accepted.status_code == 200

    second_client = TestClient(create_app(db_path))
    revision = second_client.get(f"/documents/{doc_id}/revisions/1")
    assert revision.status_code == 200
    assert revision.json()["content"] == "durable text"
    result = second_client.get(f"/suggestion-groups/{group['id']}/result")
    assert result.status_code == 200
    assert result.json()["request_id"] == "p1"
