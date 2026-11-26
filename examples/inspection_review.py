#!/usr/bin/env python3
"""Supervisor walk-through: pending re-reviews -> one work order -> closure.

Start the API first:  ./run.sh
Then run:             python3 examples/inspection_review.py
"""

from __future__ import annotations

import os
from typing import Any

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def must(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


def create_doc(client: httpx.Client, title: str, content: str) -> str:
    return must(
        client.post("/documents", json={"title": title, "content": content})
    )["id"]


def publish(client: httpx.Client, doc_id: str, revision: int = 0) -> str:
    ticket = must(
        client.post(
            "/release-tickets",
            json={"items": [{"document_id": doc_id, "revision": revision}]},
        )
    )
    released = must(
        client.post(f"/release-tickets/{ticket['ticket_id']}/confirm", json={})
    )
    return released["items"][0]["public_version_id"]


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        # 1. Publish edition 1 of a manual.
        manual = create_doc(client, "液压手册", "第 1 版：扭矩 90 Nm")
        edition_1 = publish(client, manual)

        # 2. Two field inspections on the same asset/check item, both done with
        #    edition 1.  They are independent records and can coexist.
        for suffix in ("a", "b"):
            must(
                client.post(
                    "/inspections",
                    json={
                        "asset_id": "press-4",
                        "check_item": "hydraulic-torque",
                        "public_version_id": edition_1,
                    },
                )
            )
        print("registered 2 inspection records against", edition_1)

        # 3. Edition 1 turns out to be wrong; retracting it makes both records
        #    pending (unreadable basis).  Their records are NOT erased.
        must(
            client.post(
                f"/public/versions/{edition_1}/retract",
                json={"reason": "刹车扭矩勘误"},
            )
        )
        pending = must(client.get("/inspections/pending"))
        print("pending after retraction:", pending["count"])
        print("unreadable basis:", pending["unreadable_bases"][0])

        # 4. Supervisor opens ONE work order for the whole scope.  It snapshots
        #    both pending rounds.  Opening with no pending work returns 409.
        order = must(
            client.post(
                "/review-work-orders",
                json={
                    "asset_id": "press-4",
                    "check_item": "hydraulic-torque",
                    "manual_document_id": manual,
                },
            )
        )
        print("work order captured", order["captured_count"], "rounds")

        # 5. A corrected edition 2 is published, and the supervisor re-checks
        #    the asset using that exact public version.
        first_revision = must(client.get(f"/documents/{manual}"))["current_revision"]
        first_content = must(client.get(f"/documents/{manual}"))["content"]
        must(
            client.post(
                f"/documents/{manual}/edits",
                json={
                    "base_revision": first_revision,
                    "start": first_content.index("90"),
                    "end": first_content.index("90") + 2,
                    "replacement": "120",
                },
            )
        )
        edition_2 = publish(client, manual, revision=first_revision + 1)

        result = must(
            client.post(
                f"/review-work-orders/{order['work_order_id']}/complete",
                json={"public_version_id": edition_2},
            )
        )
        print("closed records:", result["closed_count"])
        print("new current basis:", edition_2)

        # 6. Retrying the same completion replays the first result and does not
        #    touch any later pending round.
        replay = client.post(
            f"/review-work-orders/{order['work_order_id']}/complete",
            json={"public_version_id": edition_2},
        )
        print("retry status:", replay.status_code,
              "replayed:", replay.headers.get("Idempotent-Replayed"))

        # 7. Supervisor can always retrieve the snapshotted records and result.
        detail = must(
            client.get(f"/review-work-orders/{order['work_order_id']}")
        )
        print("order status:", detail["status"],
              "| captured:", detail["captured_count"],
              "| closed:", detail["result"]["closed_count"])

        # 8. The historical check evidence (registered edition 1) remains.
        record_id = detail["captured_records"][0]["record_id"]
        record = must(client.get(f"/inspections/{record_id}"))
        print("registered evidence still:", record["registered_public_version_id"])
        print("current basis now:", record["current_basis_public_version_id"])
        print("pending remaining:", must(client.get("/inspections/pending"))["count"])


if __name__ == "__main__":
    main()
