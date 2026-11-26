#!/usr/bin/env python3
"""End-to-end verification over real HTTP + a restarted SQLite service.

Run from the project root:

    python3 verify.py

The script starts Uvicorn on an ephemeral local port, exercises the required
scenarios with httpx, restarts the server against the same SQLite file, and
exits non-zero if any assertion fails.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # server is still starting
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"server did not become healthy: {last_error}")


def start_server(db_path: Path) -> tuple[subprocess.Popen[bytes], str, int]:
    port = find_free_port()
    env = os.environ.copy()
    env["DOC_REVIEW_DB"] = str(db_path)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "doc_review.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_health(base_url)
    except Exception:
        process.terminate()
        output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
        raise RuntimeError(f"server failed to start.\n{output}")
    return process, base_url, port


def stop_server(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.http = httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self.http.close()

    def call(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        **fields: Any,
    ) -> httpx.Response:
        body = json if json is not None else (fields or None)
        return self.http.request(method, path, json=body)

    def expect(
        self,
        method: str,
        path: str,
        expected: int | set[int],
        **json: Any,
    ) -> dict[str, Any]:
        response = self.call(method, path, **json)
        expected_set = {expected} if isinstance(expected, int) else set(expected)
        if response.status_code not in expected_set:
            raise AssertionError(
                f"{method} {path} expected {expected_set}, got "
                f"{response.status_code}: {response.text}"
            )
        return response.json()

    def create_doc(self, content: str, title: str = "verify") -> str:
        body = self.expect("POST", "/documents", 201, title=title, content=content)
        return str(body["id"])

    def current(self, doc_id: str) -> dict[str, Any]:
        return self.expect("GET", f"/documents/{doc_id}", 200)

    def edit_current_text(
        self,
        doc_id: str,
        old: str,
        new: str,
        request_id: str,
        *,
        at: int | None = None,
    ) -> dict[str, Any]:
        doc = self.current(doc_id)
        content = doc["content"]
        start = content.index(old) if at is None else at
        end = start + len(old)
        return self.expect(
            "POST",
            f"/documents/{doc_id}/edits",
            201,
            base_revision=doc["current_revision"],
            start=start,
            end=end,
            replacement=new,
            request_id=request_id,
        )


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def verify_prefix_mapping_and_accepted_group_visibility(client: Client) -> tuple[str, str]:
    section("1. Multi-round prefix edits, then correct suggestion replacement")
    original = "前言：旧前缀。需要替换的目标短语位于此处。结尾。"
    target = "需要替换的目标短语"
    replacement = "已正确落下的新短语"
    doc_id = client.create_doc(original, "prefix-drift")
    target_start = original.index(target)

    group = client.expect(
        "POST",
        f"/documents/{doc_id}/suggestion-groups",
        201,
        base_revision=0,
        suggestions=[
            {
                "client_id": "target-1",
                "start": target_start,
                "end": target_start + len(target),
                "replacement": replacement,
            }
        ],
    )
    group_id = group["id"]

    client.edit_current_text(doc_id, "", "[第一轮插入] ", "prefix-edit-1", at=0)
    client.edit_current_text(
        doc_id, "旧前缀", "经过第二轮扩展的新前缀", "prefix-edit-2"
    )
    client.edit_current_text(doc_id, "前言：", "", "prefix-edit-3")

    located = client.expect("GET", f"/suggestion-groups/{group_id}", 200)
    item = located["items"][0]
    assert located["drift_clean"] is True
    assert item["conflict"] is False
    assert item["current_text"] == target
    print("mapped target after 3 prefix edits:", item["current_range"], item["current_text"])

    accepted = client.expect(
        "POST",
        f"/suggestion-groups/{group_id}/accept",
        200,
        request_id="accept-prefix-1",
    )

    expected = "[第一轮插入] " + original
    expected = expected.replace("旧前缀", "经过第二轮扩展的新前缀", 1)
    expected = expected.replace("前言：", "", 1)
    expected = expected.replace(target, replacement, 1)
    assert accepted["content"] == expected
    assert accepted["revision"] == 4
    assert replacement in accepted["content"]
    assert target not in accepted["content"]
    print("final content:", accepted["content"])

    rev0 = client.expect("GET", f"/documents/{doc_id}/revisions/0", 200)
    assert rev0["content"] == original
    print("revision 0 remains readable:", rev0["revision"])

    section("1b. A successful accept also moves other pending suggestions")
    doc2_content = "ALPHA beta tail"
    doc2 = client.create_doc(doc2_content, "accepted-edit-participates")
    alpha_start = doc2_content.index("ALPHA")
    beta_start = doc2_content.index("beta")
    other_group = client.expect(
        "POST",
        f"/documents/{doc2}/suggestion-groups",
        201,
        base_revision=0,
        suggestions=[
            {
                "client_id": "alpha",
                "start": alpha_start,
                "end": alpha_start + len("ALPHA"),
                "replacement": "A-LONG-ALPHA",
            }
        ],
    )
    beta_group = client.expect(
        "POST",
        f"/documents/{doc2}/suggestion-groups",
        201,
        base_revision=0,
        suggestions=[
            {
                "client_id": "beta",
                "start": beta_start,
                "end": beta_start + len("beta"),
                "replacement": "BETA",
            }
        ],
    )
    client.expect(
        "POST",
        f"/suggestion-groups/{other_group['id']}/accept",
        200,
        request_id="accept-alpha",
    )
    beta_after = client.expect("GET", f"/suggestion-groups/{beta_group['id']}", 200)
    beta_item = beta_after["items"][0]
    assert beta_after["drift_clean"] is True
    assert beta_item["current_text"] == "beta"
    beta_accept = client.expect(
        "POST",
        f"/suggestion-groups/{beta_group['id']}/accept",
        200,
        request_id="accept-beta",
    )
    assert beta_accept["content"] == "A-LONG-ALPHA BETA tail"
    print("beta shifted through accepted alpha revision:", beta_item["current_range"])

    return doc_id, group_id


def verify_group_conflict_is_atomic(client: Client) -> None:
    section("2. One conflicting item rejects the whole suggestion group")
    original = "AAA BBB CCC DDD"
    doc_id = client.create_doc(original, "atomic-conflict")
    bbb_start = original.index("BBB")
    ddd_start = original.index("DDD")

    group = client.expect(
        "POST",
        f"/documents/{doc_id}/suggestion-groups",
        201,
        base_revision=0,
        suggestions=[
            {
                "client_id": "change-bbb",
                "start": bbb_start,
                "end": bbb_start + 3,
                "replacement": "BBB-FIX",
            },
            {
                "client_id": "change-ddd",
                "start": ddd_start,
                "end": ddd_start + 3,
                "replacement": "DDD-FIX",
            },
        ],
    )
    group_id = group["id"]

    client.edit_current_text(doc_id, "BBB", "BBB-AUTHOR-REWRITE", "conflict-author-edit")

    response = client.call(
        "POST",
        f"/suggestion-groups/{group_id}/accept",
        json={"request_id": "accept-conflict-1"},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "suggestion_conflicts"
    assert error["conflict_indexes"] == [0]
    assert error["items"][0]["conflict"] is True
    assert error["items"][0]["conflicts"][0]["code"] == "intersecting_edit"
    assert error["items"][1]["conflict"] is False
    assert error["items"][1]["current_text"] == "DDD"
    print("conflicting item:", error["items"][0]["current_range"])
    print("clean but not applied item:", error["items"][1]["current_range"])

    doc = client.current(doc_id)
    assert doc["current_revision"] == 1
    assert "DDD-FIX" not in doc["content"]
    assert "BBB-FIX" not in doc["content"]
    assert doc["content"] == "AAA BBB-AUTHOR-REWRITE CCC DDD"

    revisions = client.expect("GET", f"/documents/{doc_id}/revisions", 200)
    assert len(revisions["revisions"]) == 2  # initial + author edit only

    stored_failure = client.call(
        "POST",
        f"/suggestion-groups/{group_id}/accept",
        json={"request_id": "accept-conflict-1"},
    )
    assert stored_failure.status_code == 409
    assert stored_failure.headers.get("idempotent-replayed") == "true"
    assert stored_failure.json() == response.json()

    strict_create = client.call(
        "POST",
        f"/documents/{doc_id}/suggestion-groups?strict=true",
        json={
            "base_revision": 0,
            "suggestions": [
                {"start": bbb_start, "end": bbb_start + 3, "replacement": "X"}
            ],
        },
    )
    assert strict_create.status_code == 409
    assert strict_create.json()["error"]["code"] == "suggestion_drift_conflict"
    print("creation-time strict check returned re-review location")


def accept_concurrently(
    client: Client, group_id: str, request_id_value: str, barrier: threading.Barrier
) -> httpx.Response:
    barrier.wait()
    return client.http.post(
        f"/suggestion-groups/{group_id}/accept",
        json={"request_id": request_id_value},
    )


def verify_race_and_close(client: Client) -> None:
    section("3. Concurrent accepts: distinct request IDs have one winner")
    for round_index in range(3):
        content = f"RACE-{round_index} alpha beta gamma"
        doc_id = client.create_doc(content, f"race-{round_index}")
        beta_start = content.index("beta")
        group_id = client.expect(
            "POST",
            f"/documents/{doc_id}/suggestion-groups",
            201,
            base_revision=0,
            suggestions=[
                {
                    "start": beta_start,
                    "end": beta_start + 4,
                    "replacement": "BETA-ACCEPTED",
                }
            ],
        )["id"]

        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    accept_concurrently,
                    client,
                    group_id,
                    f"race-{round_index}-{who}",
                    barrier,
                )
                for who in ("user-a", "user-b")
            ]
            responses = [future.result() for future in futures]

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200, 409], [response.text for response in responses]
        success = next(r.json() for r in responses if r.status_code == 200)
        rejected = next(r.json()["error"] for r in responses if r.status_code == 409)
        assert rejected["code"] == "suggestion_group_already_accepted"
        assert rejected["winning_request_id"] == success["request_id"]

        doc = client.current(doc_id)
        assert doc["current_revision"] == 1
        revisions = client.expect("GET", f"/documents/{doc_id}/revisions", 200)
        assert len(revisions["revisions"]) == 2
        print(
            f"round {round_index}: winner={success['request_id']} "
            f"revision={success['revision']}; loser={rejected['code']}"
        )

        retry = client.http.post(
            f"/suggestion-groups/{group_id}/accept",
            json={"request_id": success["request_id"]},
        )
        assert retry.status_code == 200
        assert retry.headers.get("idempotent-replayed") == "true"
        assert retry.json() == success

    section("3b. The same request ID fired twice concurrently returns one first result")
    content = "SAME-ID RACE target"
    doc_id = client.create_doc(content, "same-id-race")
    target_start = content.index("target")
    group_id = client.expect(
        "POST",
        f"/documents/{doc_id}/suggestion-groups",
        201,
        base_revision=0,
        suggestions=[
            {
                "start": target_start,
                "end": target_start + len("target"),
                "replacement": "landing",
            }
        ],
    )["id"]
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(accept_concurrently, client, group_id, "same-request", barrier)
            for _ in range(2)
        ]
        same_id_responses = [future.result() for future in futures]
    assert all(response.status_code == 200 for response in same_id_responses)
    assert len({response.text for response in same_id_responses}) == 1
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in same_id_responses) == 1
    assert client.current(doc_id)["current_revision"] == 1
    print("both duplicate requests received the same stored result; one revision created")

    section("3c. Closed groups cannot be accepted")
    closed_doc = client.create_doc("closed target here", "closed")
    start = "closed target here".index("target")
    closed_group = client.expect(
        "POST",
        f"/documents/{closed_doc}/suggestion-groups",
        201,
        base_revision=0,
        suggestions=[
            {"start": start, "end": start + 6, "replacement": "new target"}
        ],
    )["id"]
    closed = client.expect(
        "POST", f"/suggestion-groups/{closed_group}/close", 200, reason="withdrawn"
    )
    assert closed["status"] == "closed"
    rejected = client.http.post(
        f"/suggestion-groups/{closed_group}/accept",
        json={"request_id": "after-close"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "suggestion_group_closed"
    print("accept after close:", rejected.json()["error"]["code"])


def verify_restart_persistence(db_path: Path, doc_id: str, group_id: str) -> None:
    section("7. Restart server and confirm revision/suggestion history survived")
    process, base_url, _ = start_server(db_path)
    try:
        client = Client(base_url)
        try:
            doc = client.current(doc_id)
            assert doc["current_revision"] == 4
            group = client.expect("GET", f"/suggestion-groups/{group_id}", 200)
            assert group["status"] == "accepted"
            assert group["accepted_revision"] == 4
            result = client.expect(
                "GET", f"/suggestion-groups/{group_id}/result", 200
            )
            assert result["revision"] == 4
            assert result["accepted"] is True

            retried = client.http.post(
                f"/suggestion-groups/{group_id}/accept",
                json={"request_id": "accept-prefix-1"},
            )
            assert retried.status_code == 200
            assert retried.headers.get("idempotent-replayed") == "true"
            assert retried.json()["revision"] == 4

            revisions = client.expect("GET", f"/documents/{doc_id}/revisions", 200)
            assert len(revisions["revisions"]) == 5
            print("after restart: current revision", doc["current_revision"])
            print("stored accept result revision:", result["revision"])
        finally:
            client.close()
    finally:
        stop_server(process)


# ---------------------------------------------------------------------------
# Release-ticket / public-reading scenarios
# ---------------------------------------------------------------------------


def prepare_ticket(
    client: Client, items: list[dict[str, Any]], expected: int = 201, **extra: Any
) -> dict[str, Any]:
    body = {"items": items, **extra}
    response = client.call("POST", "/release-tickets", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def confirm_ticket(
    client: Client, ticket_id: str, expected: int = 201
) -> dict[str, Any]:
    response = client.call(
        "POST", f"/release-tickets/{ticket_id}/confirm", json={}
    )
    assert response.status_code == expected, response.text
    return response.json()


def release_one(
    client: Client, document_id: str, revision: int = 0
) -> dict[str, Any]:
    ticket = prepare_ticket(
        client, [{"document_id": document_id, "revision": revision, "references": []}]
    )
    released = confirm_ticket(client, ticket["ticket_id"])
    return released["items"][0]


def verify_release_prepare_validation(client: Client) -> None:
    section("4. Preparing a ticket locks versions and rejects broken batches")

    c = client.create_doc("C 正文", "rel-c")
    b = client.create_doc("B 正文", "rel-b")
    a = client.create_doc("A 正文", "rel-a")
    draft = client.create_doc("内部草稿", "rel-draft")

    # Never released: unreleased content has no public entry.
    missing = client.call("GET", f"/public/documents/{draft}")
    assert missing.status_code == 404, missing.text

    c_release = release_one(client, c)
    c_public_id = c_release["public_version_id"]
    b_release = release_one(client, b)  # externals later point at B's version

    # Reject a missing external public version.
    rejected = prepare_ticket(
        client,
        [{"document_id": a, "revision": 0, "references": [draft]}],
        expected=409,
    )
    assert (
        rejected["error"]["problems"][0]["code"]
        == "external_document_has_no_public_version"
    )

    # Reject a ticket-internal cycle and name the documents in the cycle.
    cycle = prepare_ticket(
        client,
        [
            {"document_id": a, "revision": 0, "references": [b]},
            {"document_id": b, "revision": 0, "references": [a]},
        ],
        expected=409,
    )
    cycle_problem = next(
        problem
        for problem in cycle["error"]["problems"]
        if problem["code"] == "cyclic_reference"
    )
    assert set(cycle_problem["document_ids"][:-1]) == {a, b}
    print("cycle rejected:", cycle_problem["document_ids"])

    # Prepare the ticket first; it locks the latest public versions of B and C
    # that exist right now.
    ticket = prepare_ticket(
        client,
        [
            {
                "document_id": a,
                "revision": 0,
                "references": [b, c],
            },
        ],
    )
    assert ticket["status"] == "prepared"
    (a_item,) = ticket["items"]
    # Prepare reports the locked revisions and external public versions.
    assert a_item["revision"] == 0
    assert a_item["references"][0]["document_id"] == b
    assert a_item["references"][0]["public_version_id"] == b_release["public_version_id"]
    assert a_item["references"][1]["document_id"] == c
    assert a_item["references"][1]["public_version_id"] == c_public_id
    print(
        "prepared locks:",
        [(item["document_id"], item["revision"]) for item in ticket["items"]],
        "external:",
        ticket["external_locks"],
    )

    # Now the author edits B and a *different* ticket publishes B's newer
    # revision before this ticket is confirmed.  That must not replace this
    # ticket's locked B public version.
    client.edit_current_text(b, "B 正文", "B 第二版", "rel-b-edit")
    b_newer = release_one(client, b, revision=1)
    assert b_newer["public_version_id"] != b_release["public_version_id"]

    released = confirm_ticket(client, ticket["ticket_id"])
    ids = {item["document_id"]: item["public_version_id"] for item in released["items"]}
    a_public_id = ids[a]

    a_public = client.expect("GET", f"/public/versions/{a_public_id}", 200)
    assert a_public["content"] == "A 正文"
    assert a_public["references"][0]["public_version_id"] == b_release["public_version_id"]
    assert a_public["references"][1]["public_version_id"] == c_public_id

    latest = client.expect("GET", f"/public/documents/{a}", 200)
    assert latest["public_version_id"] == a_public_id
    assert [ref["ref_index"] for ref in latest["references"]] == [0, 1]

    # Re-confirm only replays; the latest public entry stays unchanged.
    replay = client.call("POST", f"/release-tickets/{ticket['ticket_id']}/confirm", json={})
    assert replay.status_code == 200
    assert replay.headers.get("idempotent-replayed") == "true"
    assert replay.json() == released
    assert (
        client.expect("GET", f"/public/documents/{a}", 200)["public_version_id"]
        == a_public_id
    )
    print("confirm replay returned the original public version IDs")
    # The retraction scenario withdraws the B public version A locked, which is
    # the earlier B version, fixed at prepare time.
    return a, a_public_id, b, c, c_public_id, b_release["public_version_id"]


def verify_retraction(
    client: Client,
    *,
    a_id: str,
    a_public_id: str,
    b_id: str,
    b_old_public_id: str,
    c_id: str,
    c_public_id: str,
) -> None:
    section("5. Retracting one public version hides referencing versions only")

    # B's old version is referenced by A's public entry.  Retract it.
    retraction = client.call(
        "POST", f"/public/versions/{b_old_public_id}/retract", json={"reason": "bad"}
    )
    assert retraction.status_code == 200, retraction.text
    assert c_public_id not in retraction.json()["affected_public_version_ids"]

    # A references B(old) directly, so A is unreadable - even though A itself
    # was never retracted and B has a newer readable public version.
    a_direct = client.call("GET", f"/public/versions/{a_public_id}")
    assert a_direct.status_code == 409, a_direct.text
    assert b_old_public_id in a_direct.json()["error"]["blocked_public_version_ids"]
    a_latest = client.call("GET", f"/public/documents/{a_id}")
    assert a_latest.status_code == 409, a_latest.text

    # The explicitly retracted old B version is unreadable, but B's newer
    # version and unrelated C are still readable.
    assert client.call("GET", f"/public/versions/{b_old_public_id}").status_code == 409
    b_latest = client.expect("GET", f"/public/documents/{b_id}", 200)
    assert b_latest["public_version_id"] != b_old_public_id
    assert client.expect("GET", f"/public/versions/{c_public_id}", 200)["content"] == "C 正文"

    # Retraction targets a version, not the document or author revisions.
    revision = client.expect("GET", f"/documents/{b_id}/revisions/0", 200)
    assert revision["content"] == "B 正文"
    draft_ok = client.expect("GET", f"/documents/{b_id}", 200)
    assert draft_ok["content"] == "B 第二版"
    # B's newer, unrelated public version is still readable.
    newer = client.expect("GET", f"/public/documents/{b_id}", 200)
    assert newer["public_version_id"] != b_old_public_id
    assert newer["content"] == "B 第二版"
    print("retraction affected only referencing public versions")


def verify_restart_release_state(
    db_path: Path,
    *,
    a_id: str,
    a_public_id: str,
    b_id: str,
    b_old_public_id: str,
    c_public_id: str,
) -> None:
    section("8. Restart keeps public locks, retraction, and release history")
    process, base_url, _ = start_server(db_path)
    try:
        client = Client(base_url)
        try:
            # Latest public entry for A is still hidden after restart.
            blocked = client.call("GET", f"/public/documents/{a_id}")
            assert blocked.status_code == 409
            assert b_old_public_id in blocked.json()["error"]["blocked_public_version_ids"]
            assert client.call("GET", f"/public/versions/{a_public_id}").status_code == 409

            # The explicit retraction survived, and the unrelated version did too.
            assert client.call("GET", f"/public/versions/{b_old_public_id}").status_code == 409
            unrelated = client.expect("GET", f"/public/versions/{c_public_id}", 200)
            assert unrelated["content"] == "C 正文"

            # A's author text and revisions survived independently.
            author = client.expect("GET", f"/documents/{b_id}", 200)
            assert author["content"] == "B 第二版"

            versions = client.expect("GET", f"/public/documents/{b_id}/versions", 200)
            statuses = {
                entry["public_version_id"]: entry["readable"]
                for entry in versions["public_versions"]
            }
            assert statuses[b_old_public_id] is False
            print("after restart: A latest blocked, B old retracted, author text intact")
        finally:
            client.close()
    finally:
        stop_server(process)


def verify_confirm_retract_race(client: Client) -> None:
    section("6. Concurrent confirm vs retract has one consistent outcome")

    for round_index in range(3):
        external = client.create_doc(f"外部 {round_index}", f"race-ext-{round_index}")
        external_release = release_one(client, external)
        external_version = external_release["public_version_id"]

        target = client.create_doc(f"目标 {round_index}", f"race-main-{round_index}")
        ticket = prepare_ticket(
            client,
            [{"document_id": target, "revision": 0, "references": [external]}],
        )
        ticket_id = ticket["ticket_id"]
        barrier = threading.Barrier(2)

        def call_confirm() -> httpx.Response:
            barrier.wait()
            return client.http.post(f"/release-tickets/{ticket_id}/confirm", json={})

        def call_retract() -> httpx.Response:
            barrier.wait()
            return client.http.post(
                f"/public/versions/{external_version}/retract", json={}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirm_future = pool.submit(call_confirm)
            retract_future = pool.submit(call_retract)
            confirm_response = confirm_future.result()
            retract_response = retract_future.result()

        assert retract_response.status_code == 200, retract_response.text
        assert confirm_response.status_code in {201, 409}, confirm_response.text

        if confirm_response.status_code == 201:
            new_version = confirm_response.json()["items"][0]["public_version_id"]
            hidden = client.call("GET", f"/public/versions/{new_version}")
            assert hidden.status_code == 409, hidden.text
            assert external_version in hidden.json()["error"]["blocked_public_version_ids"]
            outcome = "confirm-first -> new version hidden"
        else:
            assert confirm_response.json()["error"]["code"] == "release_blocked_by_retraction"
            assert client.call("GET", f"/public/documents/{target}").status_code == 404
            outcome = "retract-first -> confirm blocked"
        print(f"round {round_index}: {outcome}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="doc-review-verify-") as temp_dir:
        db_path = Path(temp_dir) / "review.db"
        process, base_url, _ = start_server(db_path)
        client = Client(base_url)
        release_state: dict[str, str] = {}
        try:
            doc_id, group_id = verify_prefix_mapping_and_accepted_group_visibility(client)
            verify_group_conflict_is_atomic(client)
            verify_race_and_close(client)
            (
                a_id,
                a_public_id,
                b_id,
                _c_id,
                c_public_id,
                b_old_public_id,
            ) = verify_release_prepare_validation(client)
            verify_retraction(
                client,
                a_id=a_id,
                a_public_id=a_public_id,
                b_id=b_id,
                b_old_public_id=b_old_public_id,
                c_public_id=c_public_id,
                c_id=_c_id,
            )
            verify_confirm_retract_race(client)
            release_state = {
                "a_id": a_id,
                "a_public_id": a_public_id,
                "b_id": b_id,
                "b_old_public_id": b_old_public_id,
                "c_public_id": c_public_id,
            }
        finally:
            client.close()
            stop_server(process)

        verify_restart_persistence(db_path, doc_id, group_id)
        verify_restart_release_state(db_path, **release_state)

    print("\nALL AUTOMATED CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
