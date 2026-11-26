from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from doc_review.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "release-test.db"))


def create_document(
    client: TestClient, content: str, title: str = "doc"
) -> str:
    response = client.post("/documents", json={"content": content, "title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def edit(client: TestClient, doc_id: str, old: str, new: str, request_id: str) -> None:
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


def prepare(
    client: TestClient,
    items: list[dict[str, Any]],
    expected: int = 201,
    request_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"items": items}
    if request_id:
        body["request_id"] = request_id
    response = client.post("/release-tickets", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def confirm(
    client: TestClient, ticket_id: str, expected: int = 201
) -> dict[str, Any]:
    response = client.post(f"/release-tickets/{ticket_id}/confirm", json={})
    assert response.status_code == expected, response.text
    return response.json()


def latest_public(client: TestClient, doc_id: str) -> dict[str, Any]:
    response = client.get(f"/public/documents/{doc_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_prepare_locks_revision_and_references_become_ordered_public_versions(
    client: TestClient,
) -> None:
    intro = create_document(client, "intro v1", "intro")
    body = create_document(client, "body v1", "body")
    appendix = create_document(client, "appendix v1", "appendix")

    # First release only publishes the appendix, so it is an external doc for
    # the next ticket.  It is retracted later in another test; here only the
    # lock value matters.
    appendix_ticket = prepare(
        client, [{"document_id": appendix, "revision": 0, "references": []}]
    )
    appendix_confirm = confirm(client, appendix_ticket["ticket_id"])
    appendix_version = appendix_confirm["items"][0]["public_version_id"]

    # Author keeps editing after prepare.
    client.post(
        f"/documents/{body}/edits",
        json={"base_revision": 0, "start": 0, "end": 0, "replacement": "> "},
    )

    ticket = prepare(
        client,
        [
            {
                "document_id": intro,
                "revision": 0,
                "references": [body, appendix],
            },
            {
                "document_id": body,
                "revision": 0,  # selected revision, not the later author draft
                "references": [],
            },
        ],
    )
    assert ticket["status"] == "prepared"
    # Editor can see every locked revision and external public-version ID.
    items = ticket["items"]
    assert [item["document_id"] for item in items] == [intro, body]
    assert [item["revision"] for item in items] == [0, 0]
    assert items[0]["references"][0]["same_ticket"] is True
    assert items[0]["references"][0]["ref_index"] == 0
    assert items[0]["references"][1]["public_version_id"] == appendix_version
    assert ticket["external_locks"] == [
        {
            "document_id": appendix,
            "revision": 0,
            "public_version_id": appendix_version,
        }
    ]

    released = confirm(client, ticket["ticket_id"])
    assert released["status"] == "released"
    ids = {
        item["document_id"]: item["public_version_id"] for item in released["items"]
    }
    assert set(ids) == {intro, body}

    # Each public version returns its own selected content and frozen ordered
    # direct references - never a later revision.
    public_body = latest_public(client, body)
    assert public_body["content"] == "body v1"  # revision 0 snapshot, not "> body v1"
    assert public_body["revision"] == 0
    assert public_body["references"] == []

    public_intro = client.get(f"/public/versions/{ids[intro]}").json()
    assert public_intro["content"] == "intro v1"
    assert [ref["public_version_id"] for ref in public_intro["references"]] == [
        ids[body],
        appendix_version,
    ]
    assert public_intro["references"][0]["ref_index"] == 0
    assert public_intro["references"][1]["ref_index"] == 1
    # The reference stays pinned to the appendix's earlier public version, even
    # though the appendix has its own latest public version from the first
    # ticket - prepare must not silently swap in a newer one.
    assert public_intro["references"][1]["public_version_id"] == appendix_version


def test_ticket_internal_reference_uses_selected_revision_not_latest_public(
    client: TestClient,
) -> None:
    referenced = create_document(client, "v0")
    referencing = create_document(client, "v0")
    first = prepare(
        client,
        [{"document_id": referenced, "revision": 0, "references": []}],
    )
    confirm(client, first["ticket_id"])

    edit(client, referenced, "v0", "v1", "ref-edit-1")
    edit(client, referenced, "v1", "v2", "ref-edit-2")

    ticket = prepare(
        client,
        [
            {"document_id": referencing, "revision": 0, "references": [referenced]},
            {"document_id": referenced, "revision": 1, "references": []},
        ],
    )
    result = confirm(client, ticket["ticket_id"])
    version = next(
        item["public_version_id"]
        for item in result["items"]
        if item["document_id"] == referencing
    )
    public = client.get(f"/public/versions/{version}").json()
    target = public["references"][0]["public_version_id"]
    target_body = client.get(f"/public/versions/{target}").json()
    # Same-ticket reference resolves to the ticket's *selected* revision (v1).
    assert target_body["content"] == "v1"


def test_prepare_rejects_missing_documents_and_revisions(client: TestClient) -> None:
    doc_id = create_document(client, "text")

    result = prepare(
        client,
        [
            {"document_id": doc_id, "revision": 0, "references": []},
            {"document_id": "doc_missing", "revision": 0, "references": []},
        ],
        expected=409,
    )
    codes = {problem["code"] for problem in result["error"]["problems"]}
    assert "document_not_found" in codes

    result = prepare(
        client,
        [{"document_id": doc_id, "revision": 99, "references": []}],
        expected=409,
    )
    assert result["error"]["problems"][0]["code"] == "revision_not_found"
    assert result["error"]["problems"][0]["revision"] == 99
    # Nothing was stored: no ticket ID exists.
    assert "ticket_id" not in result


def test_prepare_rejects_external_document_without_public_version(
    client: TestClient,
) -> None:
    doc = create_document(client, "private")
    result = prepare(
        client,
        [{"document_id": doc, "revision": 0, "references": ["doc_never_released"]}],
        expected=409,
    )
    problem = result["error"]["problems"][0]
    assert problem["code"] == "external_document_has_no_public_version"
    assert problem["ref_index"] == 0


def test_prepare_rejects_cycle_and_names_documents(client: TestClient) -> None:
    a = create_document(client, "A")
    b = create_document(client, "B")
    c = create_document(client, "C")
    result = prepare(
        client,
        [
            {"document_id": a, "revision": 0, "references": [b]},
            {"document_id": b, "revision": 0, "references": [c]},
            {"document_id": c, "revision": 0, "references": [a]},
        ],
        expected=409,
    )
    cycle = next(
        problem
        for problem in result["error"]["problems"]
        if problem["code"] == "cyclic_reference"
    )
    assert cycle["document_ids"][0] == cycle["document_ids"][-1]
    assert set(cycle["document_ids"][:-1]) == {a, b, c}

    self_ref = prepare(
        client, [{"document_id": a, "revision": 0, "references": [a]}],
        expected=409,
    )
    assert self_ref["error"]["problems"][0]["code"] == "cyclic_reference"


def test_unreleased_content_is_not_public(client: TestClient) -> None:
    doc = create_document(client, "author draft")
    assert client.get(f"/public/documents/{doc}").status_code == 404
    ticket = prepare(
        client, [{"document_id": doc, "revision": 0, "references": []}]
    )
    # Prepared but not confirmed: still not visible publicly.
    assert client.get(f"/public/documents/{doc}").status_code == 404
    # The prepared ticket itself is editor-facing, not public.
    assert client.get(f"/release-tickets/{ticket['ticket_id']}").status_code == 200


def test_confirm_blocked_when_locked_external_version_retracted(
    client: TestClient,
) -> None:
    base = create_document(client, "base")
    referencing = create_document(client, "ref")
    base_ticket = prepare(
        client, [{"document_id": base, "revision": 0, "references": []}]
    )
    base_release = confirm(client, base_ticket["ticket_id"])
    base_version = base_release["items"][0]["public_version_id"]

    ticket = prepare(
        client,
        [
            {
                "document_id": referencing,
                "revision": 0,
                "references": [base],
            }
        ],
    )
    client.post(f"/public/versions/{base_version}/retract", json={"reason": "bad"})

    blocked = confirm(client, ticket["ticket_id"], expected=409)
    error = blocked["error"]
    assert error["code"] == "release_blocked_by_retraction"
    assert base_version in error["blocked_public_version_ids"]
    assert error["blocks"][0]["locked_public_version_id"] == base_version
    # Whole ticket did not go live; no public entry appeared.
    assert client.get(f"/public/documents/{referencing}").status_code == 404


def test_confirm_blocked_when_locked_version_is_indirectly_blocked(
    client: TestClient,
) -> None:
    c = create_document(client, "C")
    b = create_document(client, "B")
    c_release = confirm(
        client,
        prepare(client, [{"document_id": c, "revision": 0, "references": []}])[
            "ticket_id"
        ],
    )
    c_version = c_release["items"][0]["public_version_id"]
    b_release = confirm(
        client,
        prepare(
            client,
            [{"document_id": b, "revision": 0, "references": [c]}],
        )["ticket_id"],
    )
    b_version = b_release["items"][0]["public_version_id"]
    a = create_document(client, "A")
    ticket = prepare(
        client, [{"document_id": a, "revision": 0, "references": [b]}]
    )

    # Retracting C transitively blocks B; A locks B, so confirm must block.
    client.post(f"/public/versions/{c_version}/retract", json={})
    blocked = confirm(client, ticket["ticket_id"], expected=409)
    assert c_version in blocked["error"]["blocked_public_version_ids"]
    assert client.get(f"/public/documents/{a}").status_code == 404


def test_confirm_blocked_by_indirect_lock_through_ticket_internal_reference(
    client: TestClient,
) -> None:
    external = create_document(client, "external")
    t_ext = prepare(
        client, [{"document_id": external, "revision": 0, "references": []}]
    )
    ext_version = confirm(client, t_ext["ticket_id"])["items"][0]["public_version_id"]

    a = create_document(client, "A")
    b = create_document(client, "B")
    ticket = prepare(
        client,
        [
            {"document_id": a, "revision": 0, "references": [b]},
            {"document_id": b, "revision": 0, "references": [external]},
        ],
    )
    client.post(f"/public/versions/{ext_version}/retract", json={})
    blocked = confirm(client, ticket["ticket_id"], expected=409)
    causes = blocked["error"]["blocks"]
    assert any(
        cause["locked_public_version_id"] == ext_version
        and cause["referenced_document_id"] == external
        for cause in causes
    )
    assert client.get(f"/public/documents/{a}").status_code == 404
    assert client.get(f"/public/documents/{b}").status_code == 404


def test_reconfirm_is_replay_and_does_not_change_latest(
    client: TestClient,
) -> None:
    doc = create_document(client, "v0")
    first_ticket = prepare(
        client, [{"document_id": doc, "revision": 0, "references": []}]
    )
    first = confirm(client, first_ticket["ticket_id"])
    version_one = first["items"][0]["public_version_id"]

    repeat = client.post(f"/release-tickets/{first_ticket['ticket_id']}/confirm", json={})
    assert repeat.status_code == 200
    assert repeat.json() == first
    assert client.get(f"/public/documents/{doc}").json()["public_version_id"] == version_one

    # A later ticket becomes the latest public entry; the old ticket's confirm
    # still replays its original result and does not touch the newer entry.
    edit(client, doc, "v0", "v1", "edit-v1")
    second_ticket = prepare(
        client, [{"document_id": doc, "revision": 1, "references": []}]
    )
    second = confirm(client, second_ticket["ticket_id"])
    version_two = second["items"][0]["public_version_id"]
    assert version_two != version_one

    again = client.post(
        f"/release-tickets/{first_ticket['ticket_id']}/confirm", json={}
    )
    assert again.status_code == 200
    assert again.json() == first
    assert client.get(f"/public/documents/{doc}").json()["public_version_id"] == version_two


def test_retract_one_version_blocks_references_but_not_unrelated_or_authors(
    client: TestClient,
) -> None:
    doc_a = create_document(client, "shared A0")
    doc_b = create_document(client, "B0")
    doc_c = create_document(client, "C0")

    ticket_one = prepare(
        client,
        [
            {"document_id": doc_a, "revision": 0, "references": []},
            {"document_id": doc_b, "revision": 0, "references": [doc_a]},
            {"document_id": doc_c, "revision": 0, "references": []},
        ],
    )
    release_one = confirm(client, ticket_one["ticket_id"])
    ids = {item["document_id"]: item["public_version_id"] for item in release_one["items"]}

    # Retract A's version only.  B references A, so B is unreadable too.
    retraction = client.post(
        f"/public/versions/{ids[doc_a]}/retract", json={"reason": "wrong"}
    )
    assert retraction.status_code == 200
    assert ids[doc_b] in retraction.json()["affected_public_version_ids"]

    assert client.get(f"/public/versions/{ids[doc_a]}").status_code == 409
    assert client.get(f"/public/versions/{ids[doc_b]}").status_code == 409
    # C is unrelated to the retracted version and stays readable.
    assert client.get(f"/public/versions/{ids[doc_c]}").status_code == 200

    # The latest public entry per document is blocked - no fallback exists -
    # and the author's revisions remain readable.
    assert client.get(f"/public/documents/{doc_b}").status_code == 409
    revisions = client.get(f"/documents/{doc_b}/revisions").json()
    assert len(revisions["revisions"]) == 1
    assert client.get(f"/documents/{doc_b}").status_code == 200
    assert client.get(f"/documents/{doc_b}/revisions/0").json()["content"] == "B0"

    # Author revisions are also unaffected for the retracted document.
    assert client.get(f"/documents/{doc_a}").json()["content"] == "shared A0"


def test_release_does_not_unblock_retracted_version(client: TestClient) -> None:
    doc = create_document(client, "v0")
    t1 = prepare(client, [{"document_id": doc, "revision": 0, "references": []}])
    r1 = confirm(client, t1["ticket_id"])
    old_version = r1["items"][0]["public_version_id"]
    client.post(f"/public/versions/{old_version}/retract", json={})

    edit(client, doc, "v0", "v1", "new-edit")
    t2 = prepare(client, [{"document_id": doc, "revision": 1, "references": []}])
    r2 = confirm(client, t2["ticket_id"])
    new_version = r2["items"][0]["public_version_id"]

    # New release becomes the latest and is readable...
    assert client.get(f"/public/documents/{doc}").json()["public_version_id"] == new_version
    # ...but the explicit old version stays blocked.
    assert client.get(f"/public/versions/{old_version}").status_code == 409
    assert old_version == r1["items"][0]["public_version_id"]


def test_newer_latest_blocked_does_not_fall_back_to_older(client: TestClient) -> None:
    doc = create_document(client, "v0")
    t1 = prepare(client, [{"document_id": doc, "revision": 0, "references": []}])
    confirm(client, t1["ticket_id"])
    edit(client, doc, "v0", "v1", "e1")
    t2 = prepare(client, [{"document_id": doc, "revision": 1, "references": []}])
    r2 = confirm(client, t2["ticket_id"])
    latest = r2["items"][0]["public_version_id"]
    client.post(f"/public/versions/{latest}/retract", json={})

    response = client.get(f"/public/documents/{doc}")
    assert response.status_code == 409
    assert response.json()["error"]["public_version_id"] == latest


def test_ticket_locked_content_survives_other_releases_and_author_edits(
    client: TestClient,
) -> None:
    referenced = create_document(client, "ref v0")
    t_ref = prepare(
        client, [{"document_id": referenced, "revision": 0, "references": []}]
    )
    ref_release = confirm(client, t_ref["ticket_id"])
    ref_version = ref_release["items"][0]["public_version_id"]

    main = create_document(client, "main v1")
    ticket = prepare(
        client,
        [{"document_id": main, "revision": 0, "references": [referenced]}],
    )
    # Another ticket supersedes the referenced doc's latest public version,
    # and the author edits `main`; the prepared ticket must still publish its
    # original snapshot and lock.
    edit(client, referenced, "ref v0", "ref v2", "ref-v2")
    t_ref2 = prepare(
        client, [{"document_id": referenced, "revision": 1, "references": []}]
    )
    confirm(client, t_ref2["ticket_id"])
    edit(client, main, "main v1", "main v2", "main-v2")

    result = confirm(client, ticket["ticket_id"])
    version = result["items"][0]["public_version_id"]
    public = client.get(f"/public/versions/{version}").json()
    assert public["content"] == "main v1"
    assert public["references"][0]["public_version_id"] == ref_version


def test_prepare_replay_request_id(client: TestClient) -> None:
    doc = create_document(client, "text")
    first = prepare(
        client,
        [{"document_id": doc, "revision": 0, "references": []}],
        request_id="prepare-1",
    )
    replay = client.post(
        "/release-tickets",
        json={
            "request_id": "prepare-1",
            "items": [{"document_id": doc, "revision": 0, "references": []}],
        },
    )
    assert replay.status_code == 201
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == first


def test_prepare_rejects_blocked_latest_external_version(
    client: TestClient,
) -> None:
    ext = create_document(client, "ext v0")
    t_ext = prepare(client, [{"document_id": ext, "revision": 0, "references": []}])
    ext_version = confirm(client, t_ext["ticket_id"])["items"][0]["public_version_id"]
    client.post(f"/public/versions/{ext_version}/retract", json={})

    referencing = create_document(client, "ref")
    result = prepare(
        client,
        [{"document_id": referencing, "revision": 0, "references": [ext]}],
        expected=409,
    )
    problem = next(
        item
        for item in result["error"]["problems"]
        if item["code"] == "external_latest_public_version_blocked"
    )
    assert problem["latest_public_version_id"] == ext_version
    assert ext_version in problem["blocked_public_version_ids"]


def test_retract_unknown_and_repeat(client: TestClient) -> None:
    assert (
        client.post("/public/versions/pub_missing/retract", json={}).status_code
        == 404
    )
    doc = create_document(client, "v0")
    t = prepare(client, [{"document_id": doc, "revision": 0, "references": []}])
    version = confirm(client, t["ticket_id"])["items"][0]["public_version_id"]
    first = client.post(f"/public/versions/{version}/retract", json={"reason": "r"})
    second = client.post(f"/public/versions/{version}/retract", json={"reason": "r2"})
    assert first.status_code == 200
    assert second.status_code == 200
    # Retracting the same explicit version twice keeps the original record.
    assert first.json()["retracted_at"] == second.json()["retracted_at"]
    assert first.json()["reason"] == "r"


def test_public_read_returns_ordered_reference_ids(client: TestClient) -> None:
    refs = [
        create_document(client, f"ref {index}") for index in range(3)
    ]
    for doc in refs:
        t = prepare(client, [{"document_id": doc, "revision": 0, "references": []}])
        confirm(client, t["ticket_id"])
    main = create_document(client, "main")
    ticket = prepare(
        client,
        [{"document_id": main, "revision": 0, "references": list(refs)}],
    )
    released = confirm(client, ticket["ticket_id"])
    main_version = released["items"][0]["public_version_id"]

    public = client.get(f"/public/versions/{main_version}").json()
    assert [ref["ref_index"] for ref in public["references"]] == [0, 1, 2]
    assert [ref["document_id"] for ref in public["references"]] == refs
    latest = client.get(f"/public/documents/{main}").json()
    assert latest["public_version_id"] == main_version
    assert [ref["public_version_id"] for ref in latest["references"]] == [
        ref["public_version_id"] for ref in public["references"]
    ]


def test_confirm_retract_race(tmp_path: Path) -> None:
    """Either retract wins (confirm blocked) or confirm wins (then hidden).

    Both orders must leave readers unable to read the new version, and the
    outcome must be one of those two - never a readable retracted version.
    """
    from concurrent.futures import ThreadPoolExecutor

    for round_index in range(5):
        race_client = TestClient(create_app(tmp_path / f"race-{round_index}.db"))
        external = create_document(race_client, f"ext-{round_index}")
        t_ext = prepare(
            race_client, [{"document_id": external, "revision": 0, "references": []}]
        )
        ext = confirm(race_client, t_ext["ticket_id"])
        ext_version = ext["items"][0]["public_version_id"]

        doc = create_document(race_client, f"main-{round_index}")
        ticket = prepare(
            race_client, [{"document_id": doc, "revision": 0, "references": [external]}]
        )
        ticket_id = ticket["ticket_id"]
        barrier = threading.Barrier(2)

        def do_confirm() -> Any:
            barrier.wait()
            return race_client.post(f"/release-tickets/{ticket_id}/confirm", json={})

        def do_retract() -> Any:
            barrier.wait()
            return race_client.post(f"/public/versions/{ext_version}/retract", json={})

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirm_future = pool.submit(do_confirm)
            retract_future = pool.submit(do_retract)
            confirm_response = confirm_future.result()
            retract_response = retract_future.result()

        assert retract_response.status_code == 200, retract_response.text
        confirm_status = confirm_response.status_code
        assert confirm_status in {201, 409}, confirm_response.text

        # Whatever the order, the reader must not be able to read the new
        # version once the referenced version is retracted.
        if confirm_status == 201:
            new_version = confirm_response.json()["items"][0]["public_version_id"]
            blocked = race_client.get(f"/public/versions/{new_version}")
            assert blocked.status_code == 409, blocked.text
            assert ext_version in blocked.json()["error"]["blocked_public_version_ids"]
        else:
            # Blocked confirm did not publish anything.
            assert (
                race_client.get(f"/public/documents/{doc}").status_code == 404
            )
        print(f"round {round_index}: confirm={confirm_status}")
