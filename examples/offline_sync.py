#!/usr/bin/env python3
"""Offline synchronization example.

Start the API first:

    ./run.sh

Then run:

    python3 examples/offline_sync.py

The script creates a small release graph, registers a device with a one-item
page cap (forcing two pages), pages a batch into an in-memory replica,
confirms it, and then pulls again to show the idle/in-sync response.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def must(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    return response.json()


def create_doc(client: httpx.Client, content: str, title: str) -> str:
    body = must(
        client.post("/documents", json={"content": content, "title": title})
    )
    return body["id"]


def release(
    client: httpx.Client, document_id: str, references: list[str] | None = None
) -> str:
    ticket = must(
        client.post(
            "/release-tickets",
            json={
                "items": [
                    {
                        "document_id": document_id,
                        "revision": 0,
                        "references": references or [],
                    }
                ]
            },
        )
    )
    result = must(
        client.post(f"/release-tickets/{ticket['ticket_id']}/confirm", json={})
    )
    return result["items"][0]["public_version_id"]


def fetch_all_pages(
    client: httpx.Client, device_id: str, first_page: dict[str, Any]
) -> list[dict[str, Any]]:
    pages = [first_page]
    sequence = first_page["sequence"]
    for page_number in range(2, first_page["page_count"] + 1):
        page = must(
            client.get(
                f"/sync/devices/{device_id}/batches/{sequence}/pages/{page_number}"
            )
        )
        pages.append(page)
    return pages


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        leaf = create_doc(client, "引用页正文 v1", "引用页")
        manual = create_doc(client, "手册正文 v1", "主手册")
        leaf_version = release(client, leaf)
        manual_version = release(client, manual, references=[leaf])

        device = must(
            client.post(
                "/sync/devices",
                json={"device_id": "example-offline-tablet", "max_page_items": 1},
            )
        )
        device_id = device["device_id"]

        must(
            client.put(
                f"/sync/devices/{device_id}/subscriptions",
                json={"document_ids": [manual]},
            )
        )

        first = must(client.post(f"/sync/devices/{device_id}/pull"))
        print(
            "batch",
            first["sequence"],
            "pages",
            first["page_count"],
            "writes",
            first["write_count"],
            "deletes",
            first["delete_count"],
        )

        pages = fetch_all_pages(client, device_id, first)

        # A real app would persist these atomically after all pages arrive.
        replica_versions: dict[str, dict[str, Any]] = {}
        for page in pages:
            for write in page["writes"]:
                replica_versions[write["public_version_id"]] = write
            for deleted_id in page["delete_ids"]:
                replica_versions.pop(deleted_id, None)

        mapping = {
            item["document_id"]: item["public_version_id"]
            for item in first["subscription_mapping"]
        }
        print("subscription mapping:", mapping)
        print("local versions:", sorted(replica_versions))
        assert mapping == {manual: manual_version}
        assert set(replica_versions) == {manual_version, leaf_version}
        assert replica_versions[manual_version]["references"] == [leaf_version]

        ack = must(
            client.post(
                f"/sync/devices/{device_id}/batches/{first['sequence']}/confirm",
                json={},
            )
        )
        print("confirmed through sequence", ack["last_ack_sequence"])

        idle = must(client.post(f"/sync/devices/{device_id}/pull"))
        print("next pull status:", idle["status"])
        assert idle["status"] == "in_sync"

        print("\nOffline sync example completed successfully ✅")


if __name__ == "__main__":
    main()
