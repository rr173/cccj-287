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
| `POST /sync/devices` | 登记离线设备，指定每页写入与删除项总数上限（1–100） |
| `PUT /sync/devices/{device_id}/subscriptions` | 整体更换该设备订阅的文档 ID 集合；设备间独立 |
| `POST|GET /sync/devices/{device_id}/pull` | 拉取当前批次首页；无变化时返回 `in_sync` |
| `GET /sync/devices/{device_id}/batches/{seq}/pages/{page}` | 重复读取同一不可变分页（页码从 1 开始） |
| `POST /sync/devices/{device_id}/batches/{seq}/confirm`（或 `/ack`） | 所有页取齐并应用后确认；重试确认幂等 |
| `GET /sync/batches/{batch_id}/pages/{page}` / `POST /sync/batches/{batch_id}/ack` | 只保存了全局批次 ID 时的等价别名 |
| `POST /inspections` | 登记一次已完成的现场检查：资产、检查项与当时所用的确切公开版 ID |
| `GET /inspections` | 按 `asset_id` / `check_item` / `manual_document_id` 筛选检查记录 |
| `GET /inspections/pending` | 列出当前待复核记录及其不可读依据、撤下原因 |
| `GET /inspections/{record_id}` | 读取单条记录、登记版、当前依据、待复核轮次与历次复核 |
| `POST /review-work-orders` | 主管按资产、检查项、手册文档开单，圈定当时全部待复核记录 |
| `GET /review-work-orders` | 按作用域/状态列出工单及其开单时圈定的记录 |
| `GET /review-work-orders/{work_order_id}` | 读取工单、圈定记录与完工结果 |
| `POST /review-work-orders/{work_order_id}/complete` | 提交同一手册仍可读的确切公开版，结掉本单仍待办的记录 |

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

## 现场检查复核（待办 → 工单 → 结案）

现场按手册某一版完成检查后，把事实登记下来；如果该手册版本后来被发现有错并撤下，检查记录本身仍保留，只把受影响的记录标成“待复核”。主管按资产、检查项和手册文档开一张工单，一次圈定当时全部待办，现场复核后用一次完工结掉它们。

### 登记已完成的检查

```bash
curl -sS -X POST "$BASE/inspections" \
  -H 'Content-Type: application/json' \
  -d '{"asset_id":"press-4","check_item":"hydraulic-torque",
       "public_version_id":"'"$EDITION_1"'"}' | jq
```

- 登记必须给出**当时所用确切公开版 ID**，且该版依据必须仍可读；不可读返回 `409 inspection_basis_unreadable`。
- `manual_document_id` 由该公开版所属文档推导，调用方不必重复提交。
- 同一 `asset_id` + `check_item` 可以有多笔**互相独立**的记录，各自保留自己的登记版。
- 每笔记录的 `registered_public_version_id` 永不改写；`current_basis_public_version_id` 初始等于登记版。
- 可选 `request_id` 让登记幂等重试。

### 撤下手册版本如何影响检查记录

```bash
curl -sS -X POST "$BASE/public/versions/$EDITION_1/retract" \
  -H 'Content-Type: application/json' -d '{"reason":"扭矩勘误"}' | jq

curl -sS "$BASE/inspections/pending" | jq
```

- 撤下与该撤下**在同一个 `BEGIN IMMEDIATE` 事务**中，依据真正变得不可读的记录立即进入待复核；并发开单/完工只看到撤下前或撤下后的完整状态。
- 依据不可读 = 依据自身被撤下，或它**直接/间接固定引用**的某个公开版被撤下。
- 进入待复核只是开启一个**待复核轮次**（round），登记版与历史依据照常可查，**不会删除任何检查记录**。
- 同一依据再增加撤下原因只扩展原轮次的 `retracted_public_version_ids`，不另开一轮。
- **新发行不会自动结案**；**撤下已被替换的旧依据**也不会让记录重新待办——只有记录当前的依据变不可读才会。
- 撤下时通过 `current_basis_public_version_id` 上的索引定位受影响记录，不逐条扫描全库检查记录。

### 主管开单

```bash
WO=$(curl -sS -X POST "$BASE/review-work-orders" \
  -H 'Content-Type: application/json' \
  -d '{"asset_id":"press-4","check_item":"hydraulic-torque",
       "manual_document_id":"'"$MANUAL"'"}' | jq -r .work_order_id)
```

- 开单时一次性圈定该 `(资产, 检查项, 手册文档)` 当下全部待复核轮次，快照到 `captured_records[]`。
- 该组**没有待办**时不建单，返回 `409 no_pending_reviews`。
- 允许不同工单圈到同一条记录/同一轮次；结案时先到先得，后到者跳过已结部分。

### 现场复核后完工

```bash
curl -sS -X POST "$BASE/review-work-orders/$WO/complete" \
  -H 'Content-Type: application/json' \
  -d '{"public_version_id":"'"$EDITION_2"'"}' | jq
```

- 必须提交**同一手册文档**中一个仍可读的确切公开版 ID。无效依据（别的文档、已撤下/间接屏蔽、缺失）返回 409/422，**既不完工也不结案**。
- 只能结掉**仍处在开单时那一轮**的待办：
  - 开单后才新增的待办不归此单；
  - 已被另一工单结案、之后又因新依据撤下而重开的待办，属于新轮次，旧单不会误结。
- 每结掉一条，该记录的 `current_basis_public_version_id` 改为本次复核所用版本，并写入一条复核历史；登记版保持不变。
- 完工结果 `closed_records[]` 列出实际结掉的记录；即使没有任何可结的（都被别的单先结），也可完工，`closed_count` 为 0。
- 相同 `public_version_id` **原样重试**返回首次结果并带 `Idempotent-Replayed: true`，不再触碰任何待办。

### 查询

```bash
# 当前待办及不可读依据
curl -sS "$BASE/inspections/pending" | jq
# 单条记录：登记版、当前依据、待复核轮次、历次复核
curl -sS "$BASE/inspections/$RECORD_ID" | jq
# 工单开单时圈定的记录与实际完工结果
curl -sS "$BASE/review-work-orders/$WO" | jq '.captured_records, .result'
```

可运行的完整示例：`python3 examples/inspection_review.py`。自动化测试见 `tests/test_inspection.py`，覆盖登记校验、直接/间接撤下、同依据多原因不另开轮、新发行不结案、旧依据撤下不重开、开单圈定、部分/全部结案、旧单不结新轮次、幂等重试、并发一致性与重启持久化。

## HTTP 离线同步

现场设备在有网时同步，随后离线使用。设备**不需要、也不会上传本地清单**；服务端依据当前公开图、设备订阅、已确认序号以及所有尚未确认但可能已被应用的批次做保守对账。

### 目标副本如何计算

对设备订阅集合中的每个文档：

1. 取该文档**当前最新且可读**的公开版作为订阅根；若从未发行、最新版被撤下或被其引用图中的撤下版本屏蔽，则该订阅项映射为 `null`，**不回退到旧公开版**。
2. 从每个可读根出发，沿公开版中冻结的、有序的公开版 ID 引用做闭包遍历。
3. 引用始终指向发行时锁定的旧版；文档后来发行的最新版不会替换被引用版本。
4. 一个旧版即使不再是任何订阅文档的最新版，只要仍在另一个保留根的固定引用闭包中，就仍属于目标，不会被删除。

每次拉取都在一个 SQLite `BEGIN IMMEDIATE` 事务中完成目标计算、失效判断和批次持久化，因此与发行确认、撤下、订阅更换交错时，一次响应只依据一个一致状态。撤下提交后才开始的取页/确认请求会看到旧批次失效，不能再取到因此不可读的正文。

### 登记、订阅和分页

登记时用 `max_page_items`（也接受 `page_limit`/`page_size`）指定每页 `writes.length + delete_ids.length` 的上限，范围为 1–100。可传自己的 `device_id`；重复登记同一 ID 且分页限制相同会回放首次响应。

```bash
BASE=http://127.0.0.1:8000

DEVICE=$(curl -sS -X POST "$BASE/sync/devices" \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"shop-tablet-01","max_page_items":2}' \
  | jq -r .device_id)

# 整体替换订阅集合；也接受原始 JSON 数组或 {"ids":[...]}。
curl -sS -X PUT "$BASE/sync/devices/$DEVICE/subscriptions" \
  -H 'Content-Type: application/json' \
  -d '{"document_ids":["'"$DOC_A"'","'"$DOC_B"'"]}' | jq

# 拉取或重试当前未确认批次的第 1 页。
curl -sS -X POST "$BASE/sync/devices/$DEVICE/pull" | jq
```

拉取响应在有变更时包含：

- `sequence` / `seq`：设备内单调递增的批次序号；
- `batch_id` / `id`：全局批次 ID；
- `page_count`、`page_number`、`next_page`；
- `subscription_mapping[]`：每项 `{document_id, public_version_id, readable}`；
- `writes[]`：按公开版 ID 给出 `content`、标题、文档/修订信息，以及有序的 `references: [public_version_id, ...]`（另有 `reference_details`）；
- `delete_ids[]`：应从本地副本删除的公开版 ID；删除不存在的 ID 无害；
- `changes[]`：把写入表示为 `op:"put"`、删除表示为 `op:"delete"` 的合并便捷视图。

客户端必须取齐 `1..page_count` 的所有页，然后才整体替换订阅映射并应用写入/删除。示例客户端：

```bash
SEQ=1
PAGE_COUNT=$(curl -sS -X POST "$BASE/sync/devices/$DEVICE/pull" | jq .page_count)
for PAGE in $(seq 1 "$PAGE_COUNT"); do
  curl -sS "$BASE/sync/devices/$DEVICE/batches/$SEQ/pages/$PAGE" > "/tmp/sync-page-$PAGE.json"
done

# 只有所有页齐全后，离线程序才提交本地事务并替换映射。
curl -sS -X POST "$BASE/sync/devices/$DEVICE/batches/$SEQ/confirm" \
  -H 'Content-Type: application/json' -d '{}' | jq
```

当目标与服务端记录的已确认目标一致且没有待处理批次时，`POST .../pull` 返回 `status:"in_sync"`、`synced:true`。

### 重试、失效和漏确认

- 目标不变时，重复 `pull` 返回同一序号、同一 `batch_id` 和同一第 1 页；重复 GET 同一页也返回相同分页。
- 服务端持久化记录已成功提供的页码。未取齐所有页时确认返回 `409 sync_batch_pages_incomplete`，并列出 `served_page_numbers` / `missing_page_numbers`。
- 新发行、撤下或订阅更换使当前目标（包括订阅映射）不同于未确认批次时：
  - 旧批次状态永久变为 `invalidated`；
  - 旧页不再返回正文，旧确认返回 `409 sync_batch_stale` / `can_revive:false`；
  - 再次 `pull` 会生成更高序号的新纠正批次。
- 已被服务端成功处理的确认，即使响应丢失、之后目标又变化，重试仍返回 200 和 `Idempotent-Replayed: true`，且 `last_ack_sequence` 不倒退。
- 客户端可以跳过旧批次；应用序号 N 后忽略所有 `<= N` 的批次/响应，并确认 N。

新批次按所有可能的本地状态保守生成。设服务端最新已确认序号为 A，则可能状态包括 A 的完整目标，以及 A 之后**所有页均已提供、因而客户端可能已集齐并应用**的批次完整目标（客户端也可能从未应用，或已应用但确认丢失/迟到）。未取齐页的批次不能被符合协议的客户端原子应用，不会作为可能的已安装状态。对这些状态：

- `delete_ids = 所有可能状态中的版本并集 − 当前目标集合`；
- `writes = 当前目标集合 − 所有可能状态中的版本交集`。

因此，从未应用旧批的设备会收到全部所需正文和无害删除；已经应用旧批但确认丢失的设备会收到缺失正文和所有可能残留 ID 的删除。连续多次确认丢失也按同一并集/交集规则收敛。

设备、订阅、批次、分页、已提供页码和确认序号都保存在现有 SQLite 数据库中；服务重启后批次回放与确认幂等性不变。

可运行的 HTTP 调用示例：

```bash
python3 examples/offline_sync.py
```

离线同步的自动化测试位于 `tests/test_sync.py`，覆盖分页上限、固定引用、订阅更换、旧版保留、撤下不回退、旧批失效、漏确认/连续漏确认对账以及重启持久化。

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

其中 `tests/test_sync.py` 专门验证离线同步协议；`tests/test_inspection.py` 验证现场检查复核流程。两者都使用 FastAPI `TestClient` 和临时 SQLite，不需要额外客户端界面。

## 主要文件

```text
doc_review/
  main.py       # FastAPI 路由与应用工厂
  service.py    # 修订、建议组、幂等和并发接受用例
  release.py    # 发行单、公开版、引用图与撤下
  inspection.py # 现场检查记录、复核待办轮次、主管工单与结案
  sync.py       # 离线设备目标图、批次/分页、失效与漏确认对账
  sync_api.py   # 离线同步 HTTP 路由
  mapping.py    # 纯区间迁移/冲突判定逻辑
  db.py         # SQLite schema、连接和立即写事务
  schemas.py    # 请求模型
verify.py       # 真实 HTTP + 并发 + 重启的端到端验证
examples/
  quickstart.py # 简短调用示例
  inspection_review.py # 主管从待办到结案的 HTTP 示例
```
