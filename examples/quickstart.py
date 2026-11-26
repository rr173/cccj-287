#!/usr/bin/env python3
"""Short HTTP example. Start the server first: ./run.sh"""

import os

import httpx

base_url = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

response = httpx.post(
    f"{base_url}/documents",
    json={"content": "前言：旧前缀。请修改目标词语。"},
    timeout=10,
)
response.raise_for_status()
doc = response.json()
doc_id = doc["id"]
content = doc["content"]
target = "目标词语"
start = content.index(target)

group = httpx.post(
    f"{base_url}/documents/{doc_id}/suggestion-groups",
    json={
        "base_revision": 0,
        "suggestions": [
            {
                "client_id": "word-1",
                "start": start,
                "end": start + len(target),  # Unicode code points
                "replacement": "新词语",
            }
        ]
    },
).raise_for_status().json()

# Author changes a prefix while the reviewer's group is bound to revision 0.
current = httpx.get(f"{base_url}/documents/{doc_id}").json()
httpx.post(
    f"{base_url}/documents/{doc_id}/edits",
    json={
        "base_revision": current["current_revision"],
        "start": 0,
        "end": 0,
        "replacement": "[作者的前缀修改] ",
    },
).raise_for_status()

located = httpx.get(f"{base_url}/suggestion-groups/{group['id']}").json()
print("located after author edit:", located["items"][0]["current_range"])

accepted = httpx.post(
    f"{base_url}/suggestion-groups/{group['id']}/accept",
    json={"request_id": "example-accept-001"},
).raise_for_status().json()
print("new revision:", accepted["revision"])
print("content:", accepted["content"])

# ---- Release a batch to readers ------------------------------------------
# Two docs; the second references the first.  Both appear only after confirm.
doc_two = httpx.post(
    f"{base_url}/documents",
    json={"title": "手册第二页", "content": "参见第一页。"},
    timeout=10,
).raise_for_status().json()

ticket = httpx.post(
    f"{base_url}/release-tickets",
    json={
        "items": [
            {"document_id": doc_id, "revision": accepted["revision"], "references": [doc_two["id"]]},
            {"document_id": doc_two["id"], "revision": 0, "references": []},
        ]
    },
    timeout=10,
).raise_for_status().json()
print("prepared ticket locks:", [(i["document_id"], i["revision"]) for i in ticket["items"]])

released = httpx.post(
    f"{base_url}/release-tickets/{ticket['ticket_id']}/confirm",
    json={},
    timeout=10,
).raise_for_status().json()
public_ids = {item["document_id"]: item["public_version_id"] for item in released["items"]}
print("public version IDs:", public_ids)

latest = httpx.get(f"{base_url}/public/documents/{doc_id}", timeout=10).raise_for_status().json()
print("reader sees:", latest["content"])
print("frozen references:", [ref["public_version_id"] for ref in latest["references"]])

# Retract the referenced page's public version; referencing entry is hidden.
httpx.post(
    f"{base_url}/public/versions/{public_ids[doc_two['id']]}/retract",
    json={"reason": "示例撤下"},
    timeout=10,
).raise_for_status()
hidden = httpx.get(f"{base_url}/public/documents/{doc_id}", timeout=10)
print("referencing entry after retraction:", hidden.status_code, hidden.json()["error"]["code"])
