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
    section("4. Restart server and confirm history/results survived")
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="doc-review-verify-") as temp_dir:
        db_path = Path(temp_dir) / "review.db"
        process, base_url, _ = start_server(db_path)
        client = Client(base_url)
        try:
            doc_id, group_id = verify_prefix_mapping_and_accepted_group_visibility(client)
            verify_group_conflict_is_atomic(client)
            verify_race_and_close(client)
        finally:
            client.close()
            stop_server(process)

        verify_restart_persistence(db_path, doc_id, group_id)

    print("\nALL AUTOMATED CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
