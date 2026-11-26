# 文档审阅后端

使用 Python + FastAPI + SQLite 实现的无界面文档审阅服务。审阅建议组可以绑定任意历史修订；作者继续编辑后，服务会沿完整修订历史迁移建议区间，并依据历史操作而不是“现在的文字是否相同”判断冲突。

服务同时提供一套**整批发行（release）**流程：编辑准备一张发行单并锁定各文档的正文修订和引用版本，确认后读者才能看到整批内容；发现错误时可以只撤下某个公开版，而不会改动作者稿或回退公开入口。详见下文「发行流程」。

## 直接运行

```bash
python3 -m pip install -r requirements.txt
./run.sh
# 等价方式：python3 main.py
# 或：DOC_REVIEW_DB=/var/lib/doc-review/review.db \
#     python3 -m uvicorn doc_review.main:app --host 127.0.0.1 --port 8000
```

默认监听 <http://127.0.0.1:8000>，SQLite 文件为 `./data/review.db`。交互式 OpenAPI 文档在 `/docs`，但业务不依赖界面。

另开终端运行简短 HTTP 示例：

```bash
python3 examples/quickstart.py
```

## 一分钟 curl 示例

```bash
BASE=http://127.0.0.1:8000

# 创建文档，初始修订为 0
DOC_ID=$(curl -sS -X POST "$BASE/documents" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Hello review world"}' | jq -r .id)

# 读取指定历史修订
curl -sS "$BASE/documents/$DOC_ID/revisions/0" | jq

# 在 [6,12) 处提出替换 "review" -> "REVIEW"
GRP_ID=$(curl -sS -X POST "$BASE/documents/$DOC_ID/suggestion-groups" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":0,"suggestions":[{"start":6,"end":12,"replacement":"REVIEW"}]}' \
  | jq -r .id)

# 作者先在文首插入前缀；此时修订 0 仍是当前版，返回 201 和 revision 1
curl -sS -X POST "$BASE/documents/$DOC_ID/edits" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":0,"start":0,"end":0,"replacement":">> "}' | jq

# 另一个作者仍基于修订 0 修改 "world" [13,18)：返回 409，
# 其中 rebased_range.current_range 已平移为 [16,21)
curl -sS -X POST "$BASE/documents/$DOC_ID/edits" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":0,"start":13,"end":18,"replacement":"earth"}' | jq

# 按 current_revision=1 和重定位区间重新提交
curl -sS -X POST "$BASE/documents/$DOC_ID/edits" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision":1,"start":16,"end":21,"replacement":"earth"}' | jq

# 查看建议在当前修订上的位置：前缀使其从 [6,12) 移到 [9,15)
curl -sS "$BASE/suggestion-groups/$GRP_ID" \
  | jq '{clean: .drift_clean, range: .items[0].current_range, text: .items[0].current_text}'

# 接受建议组；request_id 用于安全重试
curl -sS -X POST "$BASE/suggestion-groups/$GRP_ID/accept" \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"accept-001"}' | jq

# 同一 request_id 重试返回首次结果，响应头带 Idempotent-Replayed: true
curl -i -X POST "$BASE/suggestion-groups/$GRP_ID/accept" \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"accept-001"}'
```

发行一段最小示例（完整流程见下文「发行流程」）：

```bash
# 准备：锁定 DOC_ID 的修订 3，并按顺序引用 DOC_B、DOC_C
TICKET=$(curl -sS -X POST "$BASE/release-tickets" \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"document_id":"'"$DOC_ID"'","revision":3,"references":["'"$DOC_B"'","'"$DOC_C"'"]}]}' \
  | jq -r .ticket_id)

# 确认：整单上线，返回每份文档可直接读取的 public_version_id
curl -sS -X POST "$BASE/release-tickets/$TICKET/confirm" \
  -H 'Content-Type: application/json' -d '{}' | jq

# 读者按文档读取最新成功发行的版；也可用 public_version_id 读指定旧版
curl -sS "$BASE/public/documents/$DOC_ID" | jq
curl -sS "$BASE/public/versions/$PUB_ID" | jq
```

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /documents` | 创建文档，可传 `title`、`content`；初始修订号为 `0` |
| `GET /documents/{id}` | 读取文档和当前正文 |
| `GET /documents/{id}/revisions` | 列出历史修订元数据 |
| `GET /documents/{id}/revisions/{rev}` | 读取指定修订正文 |
| `POST /documents/{id}/edits` | 提交一次区间替换/插入/删除，必须带 `base_revision`、`start`、`end`、`replacement` |
| `POST /documents/{id}/suggestion-groups` | 创建绑定 `base_revision` 的建议组 |
| `GET /documents/{id}/suggestion-groups` | 列出文档的建议组 |
| `GET /suggestion-groups/{group_id}` | 查询建议组；待处理组会实时给出当前定位 |
| `POST /suggestion-groups/{group_id}/accept` | 原子接受整组，需在 body 或 `Idempotency-Key` 头中提供 `request_id` |
| `POST /suggestion-groups/{group_id}/close` | 关闭建议组；关闭后不能接受 |
| `GET /suggestion-groups/{group_id}/result` | 查阅已成功接受并落为新修订的结果 |
| `POST /release-tickets` | 准备一张发行单；校验引用并锁定正文修订与外部公开版 |
| `GET /release-tickets/{ticket_id}` | 查看发行单状态和已锁定的版本 |
| `POST /release-tickets/{ticket_id}/confirm` | 整单确认发行；重复确认只回放首次结果 |
| `GET /public/documents/{id}` | 按文档读取最后一次成功发行的公开版 |
| `GET /public/documents/{id}/versions` | 列出该文档的公开版及是否可读 |
| `GET /public/versions/{public_version_id}` | 按公开版 ID 读取正文和按序固定的直接引用 |
| `POST /public/versions/{public_version_id}/retract` | 撤下一个明确的公开版 |

### 正文编辑冲突

`POST /documents/{id}/edits` 的 `base_revision` 必须等于当前修订。过期时返回 HTTP 409：

```json
{
  "error": {
    "code": "revision_conflict",
    "base_revision": 0,
    "current_revision": 2,
    "rebased_range": {
      "conflict": false,
      "original_range": {"start": 2, "end": 5},
      "current_range": {"start": 8, "end": 11},
      "current_text": "..."
    }
  }
}
```

服务不会自动覆盖后来的编辑；调用方应按 `rebased_range` 重新审阅并以 `current_revision` 为新基准提交。可选 `request_id` 也能保证编辑请求重试返回首次结果。

### 创建建议组时的漂移检查

请求示例：

```json
{
  "base_revision": 3,
  "suggestions": [
    {"client_id": "s1", "start": 10, "end": 14, "replacement": "新文本"},
    {"client_id": "s2", "start": 20, "end": 22, "replacement": ""}
  ]
}
```

- 位置按 **Unicode 码点**计算，Python 的 `len(str)` 与字符串切片即为该口径。
- 所有区间都是 `[start, end)`；相邻但不重叠的区间合法。
- 建议区间必须非空且互不相交。
- 默认会保存语法合法的历史建议组，并在响应中给出 `drift_clean` 和逐项 `items` 定位。
- 若希望“基准版到当前版之间已有冲突就不创建”，调用 `POST .../suggestion-groups?strict=true`；此时返回 409 且不落库建议组。

### 定位与冲突规则

对于建议指向的旧区间 `[s,e)`，逐个经过后续修订中的替换操作 `[a,b) -> replacement`：

- `b <= s`：操作完全在原文前方；位置随增删平移。`a=b=s` 的起点插入也按“原文之前”处理，因此起点和终点都后移。
- `a >= e`：操作完全在原文后方；不影响区间位置。`a=b=e` 的终点插入按“原文之后”处理，终点不移动。
- `s < a=b < e`：严格位于区间内部的插入，判为冲突，错误码 `internal_insertion`。
- 其他与区间有正长度交集的删除/替换，判为冲突，错误码 `intersecting_edit`。

每次接受成功产生的修订会把该组所有已定位操作存入 `revision_ops`，因此后续建议也会穿过“接受建议”造成的修订，而不只是穿过作者编辑。

冲突响应是 HTTP 409，`error.items[]` 逐项包含：

- `original_range`：建议组基准修订中的原区间；
- `current_range`：当前修订上的可定位区间；若原文已被整段删除，可能是零宽定位点；
- `current_text`：该区间当前文字；
- `context_range` / `context_text`：定位点前后的码点上下文，方便重新审阅；
- `conflicts[]`：具体冲突修订、来源（作者编辑或接受建议）、当时的操作区间和替换文本。

历史上一旦发生过交集修改，该项会永久保留冲突记录。即使后续又把文字改回原样，`conflicts[]` 仍非空，不会被误判为未改动。

### 原子接受、幂等和并发

- 一个建议组中的任意一项冲突：HTTP 409，整组不创建新修订、正文完全不变、建议组仍为 `pending`。
- 全部可定位：所有替换落在同一份当前修订上，并只创建一个新修订。
- 接受请求必须带 `request_id` 或 `Idempotency-Key`。同一作用域下相同 ID 的重试，包括并发重试，返回数据库中保存的首次状态码和响应体，并带 `Idempotent-Replayed: true`。
- 不同请求 ID 的两个并发接受由 SQLite `BEGIN IMMEDIATE` 串行化，再用 `status='pending'` 条件更新竞争：恰好一个返回 200，另一个返回 `suggestion_group_already_accepted`。
- 已关闭组接受时返回 `suggestion_group_closed`，不会落稿。
- 所有修订、建议组、逐项建议、幂等结果和关闭原因均在 SQLite 中，重启进程后仍可查询。

## 发行流程

作者修订仍是内部草稿；读者只能通过 `/public` 读取**成功发行**产生的公开版。一张发行单要么整批上线，要么整批不上线。

### 准备发行单

```bash
curl -sS -X POST "$BASE/release-tickets" \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      {"document_id": "'"$DOC_A"'", "revision": 3, "references": ["'"$DOC_B"'", "'"$DOC_C"'"]},
      {"document_id": "'"$DOC_B"'", "revision": 1, "references": []}
    ]
  }' | jq
```

- 每项提交文档 ID、选定正文修订 `revision`，以及**按展示顺序**列出的直接引用文档 ID `references`。
- 引用**本单内**的文档时，使用该单为它选定的修订（返回中 `same_ticket=true`）。
- 引用**本单外**的文档时，锁定准备时该文档的**最新公开版**（返回中给出 `public_version_id` 与 `revision`）。
- 准备成功返回 `201`，`items[]` 和 `external_locks[]` 让编辑看到每份文档锁定了哪个修订/公开版；正文在准备时已快照。
- 任何问题都会整单拒绝并返回 `409 release_prepare_rejected`，`error.problems[]` 指出具体位置：
  - `document_not_found` / `revision_not_found`；
  - `external_document_has_no_public_version`（外部文档从未成功发行）；
  - `external_latest_public_version_blocked`（外部文档的最新公开版已不可读，直接或间接被撤下）；
  - `cyclic_reference`（单内循环引用，`document_ids` 给出具体环路）；
  - `duplicate_document_in_ticket`。
- 可选 `request_id` 让准备请求幂等重试。
- 准备完成后，作者继续改稿、或其他发行单上线，都不会替换本单已锁定的正文和引用。

### 确认发行

```bash
curl -sS -X POST "$BASE/release-tickets/$TICKET/confirm" \
  -H 'Content-Type: application/json' -d '{}' | jq
```

- 成功返回 `201`，`items[].public_version_id` 是每份文档可直接读取的公开版 ID；同一个响应里也给出各版按序固定的引用公开版 ID。
- 若本单锁定的任何外部公开版（包括它间接引用的版本）已被撤下，返回 `409 release_blocked_by_retraction`，`blocks[]` 指出被阻塞的锁，整单不上线，也不改变此前的公开入口。
- 重复确认一张已成功发行的单只返回原来的发行结果（HTTP `200`，响应头 `Idempotent-Replayed: true`），不重新上线、不改变最新公开版。

### 读者读取

```bash
# 按文档读取最后一次成功发行的版
curl -sS "$BASE/public/documents/$DOC_A" | jq

# 按公开版 ID 读取指定旧版
curl -sS "$BASE/public/versions/$PUB_ID" | jq
```

两种读法都返回该版 `content` 和按 `ref_index` 顺序固定的直接引用 `references[].public_version_id`，不会把引用换成最新版。从未成功发行的内容、准备但未确认的发行单，都不能从 `/public` 读取。

### 撤下公开版

```bash
curl -sS -X POST "$BASE/public/versions/$PUB_ID/retract" \
  -H 'Content-Type: application/json' -d '{"reason":"勘误"}' | jq
```

- 撤下的是一个**明确的公开版**，不是整份文档，也不改动作者修订。
- 撤下后，任何直接或间接引用它的公开版都不能再从 `/public` 取到正文（`409 public_version_blocked`，`blocked_public_version_ids` 列出被撤版本），即使它们不是最新版。
- 重新发行一份文档不会解除旧版的屏蔽；最新公开版被屏蔽时**不回退**到更旧的版（`GET /public/documents/{id}` 也返回 409）。
- 与被撤版本无关的公开版，以及所有作者修订（`/documents/...`），仍可读取。
- 同一公开版重复撤下是幂等的，保留首次撤下记录。

### 并发与持久化

- 确认发行与撤下都在 SQLite `BEGIN IMMEDIATE` 中决策：先撤下则确认被阻塞；先确认则撤下成功后新版本立即可被判定为屏蔽。两种顺序都不会让读者读到受影响的版本。
- 发行单、公开版、引用关系与撤下记录都持久化在 SQLite，重启后结果不变。

## 自动化验证

一键端到端验证会自己启动 Uvicorn、使用临时 SQLite 文件、发起真实 HTTP 并发请求，并验证重启持久化：

```bash
python3 verify.py
```

它展示：

1. 连续多轮前缀插入/替换/删除后，旧建议准确移动并成功替换；
2. 一次成功接受造成的位置变化会继续移动另一个待审建议；
3. 一组中一项冲突时，整组返回逐项冲突且不产生任何新修订；
4. 三个并发竞争轮次中，不同请求 ID 始终只有一个成功；
5. 同一请求 ID 的并发重试得到同一份首次结果；
6. 关闭后不能接受；
7. 准备发行单时锁定修订与外部公开版，拒绝缺公开版/循环引用，确认后引用不漂移；
8. 撤下一个公开版只屏蔽直接/间接引用它的版本，作者稿与无关公开版仍可读；
9. 并发确认与撤下只有一致结果；
10. 停止并重启服务后，历史修订、建议结果、公开版、锁定与撤下状态仍可查阅。

上面的 `verify.py` 会打印所有场景的中间结果并断言；`pytest` 另有独立的发行测试（`tests/test_release.py`）覆盖准备校验、幂等、屏蔽传播、并发与重启。

单元/接口测试：

```bash
python3 -m pytest -q
```

## 主要文件

```text
doc_review/
  main.py       # FastAPI 路由与应用工厂
  service.py    # 修订、建议组、幂等和并发接受用例
  release.py    # 发行单、公开版、引用图与撤下
  mapping.py    # 纯区间迁移/冲突判定逻辑
  db.py         # SQLite schema、连接和立即写事务
  schemas.py    # 请求模型
verify.py       # 真实 HTTP + 并发 + 重启的端到端验证
examples/
  quickstart.py # 简短调用示例
```
