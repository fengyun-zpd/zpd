# StockMind V1 使用说明书（小白版）

> 适用版本：V1 本机 Docker Compose 闭环。本文是操作手册，不改变
> `AGENTS.md`、需求规格、架构设计和 ADR 的约束。系统使用固定种子的合成数据，
> 不能直接当作真实 WMS/ERP 或真实采购系统使用。

## 1. 先知道你手里的是什么

StockMind 不是一个“输入一句话就自动下单”的聊天机器人。它把补货工作拆成几步：

1. 你用自然语言提出补货需求。
2. Agent 只负责理解、追问缺少的仓库/SKU/规划窗口，并读取证据。
3. 确定性领域服务计算预测、供应商和补货数量，生成待审批草稿。
4. 审批员逐行批准或排除。
5. 采购员按供应商创建采购单并下达。
6. 到货员分批收货，全部完成后关闭采购单。

审批、下单、收货、关闭都不是 Agent 自动完成的动作。页面上的每次写操作都会经过角色、状态、幂等和审计校验。

### 1.1 功能地图：哪些地方是 Agent，哪些地方是业务系统

| 页面/能力 | 你可以做什么 | 是否由 Agent 驱动 |
|---|---|---|
| 补货助手 | 用自然语言指定仓库、SKU/分类和 7/14/30 天窗口；补齐缺参；查看库存、需求、规则和预测证据；生成待审批草稿。 | 是，这是 Agent 主入口。 |
| 补货工作台 | 查看计划、仓库、触发方式、状态、窗口和版本。 | 否，普通查询页面。 |
| 审批箱 | 逐条批准/排除、整单驳回；遇到计划过期时重新生成。 | 否，审批业务页面。 |
| 采购单 | 从已批准计划按供应商建单；下达、取消、查询未知订单；登记分批到货；关闭。 | 否，采购业务页面。 |
| 定时任务 | 查看任务并立即触发一次扫描；后台 Beat 也会按 Cron 执行。 | 否，后台任务入口。 |
| 执行记录 | 查看每次扫描的 SKU 数、成功/阻断/失败和告警。 | 否，审计查询页面。 |
| 规则知识库 | 检索文档和结构化规则，查看来源、版本、生效信息；管理员可导入 Markdown/TXT 资料建立检索索引。 | Agent 会调用只读检索工具，但此页面本身不是聊天 Agent。导入资料只作为证据，不会直接修改补货公式。 |
| 操作人切换器 | 切换演示身份以验证权限。 | 否，不是真实登录。 |

准确地说，当前产品是一个“补货决策 Agent 嵌入仓储工作流”的 V1 纵向切片。它不是通用客服 Agent，也不会替你自动审批、自动下单或自动修改规则。

### 1.2 可以直接验证的 Agent 行为

在“补货助手”分别输入下面几句话，并观察页面状态，而不是只看有没有回复：

| 输入 | 应看到的结果 |
|---|---|
| `请检查仓库 WH-E 的 SKU-E01，按14天窗口生成补货建议。` | 读取库存/需求/供应商/规则，生成包含建议数量和证据的待审批计划。 |
| `请检查华东仓紧固件，按7天窗口补货。` | 将“华东仓”映射为 `WH-E`，将“紧固件”展开为具体 SKU 后再计算。 |
| `帮我补货 SKU-E01` | 返回“需要补充参数”，追问仓库或规划窗口，不猜测。 |
| `帮我审批这个计划并下单` | Agent 不执行审批和下单，只能引导你到审批箱/采购单页面。 |
| `检查 WH-S 的 SKU-E07，按14天窗口补货。` | 返回阻断，原因应是缺少适用规则，并给出下一步。 |
| `检查 WH-E 的 SKU-E05，按14天窗口补货。` | 返回阻断，原因应是没有候选供应商。 |

如果输入第一句后完全没有工具证据、计划号或待审批状态，才可以判定 Agent 主链路没有工作；只是页面能打开不能算 Agent 验收通过。

## 2. 第一次启动

### 2.1 前置条件

- Docker Desktop 已安装并处于运行状态。
- Docker Desktop 可以拉取镜像；如果拉取超时，先在 Docker Desktop 的代理设置中配置与浏览器一致的 HTTPS 代理。
- 项目目录为 `D:\workplace\PyCharmMiscProject\仓储`。
- `.env` 可以没有真实密钥。没有 LLM Key 时，系统使用 OFFLINE 演示模式，依然可以完成完整闭环。

不要把 API Key 写进聊天、截图、Git 提交或测试文件。曾经在聊天或截图中公开过的 Key 应立即在服务商控制台撤销并重新生成。

### 2.2 配置环境文件

在项目根目录复制 `.env.example` 为 `.env`。PowerShell 示例：

```powershell
Set-Location 'D:\workplace\PyCharmMiscProject\仓储'
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

打开 `.env` 后，以下配置可以保持为空：

```dotenv
LLM_API_KEY=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
```

如果需要真实 DeepSeek 对话，只填写自己的 Key：

```dotenv
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=你的新Key
LLM_MODEL=deepseek-chat
```

保存 `.env` 后需要重启 API 容器才会加载新值：`docker compose restart api`。

### 2.3 启动项目

在项目根目录打开 PowerShell，依次执行：

```powershell
Set-Location 'D:\workplace\PyCharmMiscProject\仓储'
docker info
docker compose up -d --build
docker compose ps
```

看到 `db`、`redis` 的状态为 `healthy`，并且 `api`、`worker`、`beat`、`frontend`、`mock-supplier` 为 `Up`，说明服务已启动。

浏览器打开：

- 工作区：<http://localhost:3000>
- API 文档：<http://localhost:8000/docs>
- 模拟供应商状态：<http://localhost:8100/fault-modes>

第一次启动会自动执行数据库迁移和合成种子初始化。启动较慢时等待一两分钟，再刷新页面。

### 2.4 启动失败时先做什么

先看服务状态和最近日志，不要先删除数据卷：

```powershell
docker compose ps
docker compose logs --tail=120 api
docker compose logs --tail=120 db
```

常见情况：

| 现象 | 处理 |
|---|---|
| `error response from daemon`、拉取镜像超时 | 检查 Docker Desktop 代理和网络，先单独重试对应 `docker pull`。 |
| `api` 反复重启 | 看 `api` 日志；通常是数据库尚未健康、`.env` 格式错误或迁移失败。 |
| 页面打不开但容器正常 | 确认访问的是 `http://localhost:3000`，再看 `frontend` 日志。 |
| 对话显示 `offline` | 没有 Key 或外部 LLM 请求失败，这是可用的降级模式，不代表本地闭环坏了。 |
| 页面显示旧数据 | 确认是否刚执行过种子重置；浏览器刷新后重新选择仓库和计划。 |

## 3. 操作人和角色

页面右上角有操作人切换器。它只是 V1 演示用的身份选择器，不等于生产登录系统。

| 操作人 | 角色 | 推荐用途 |
|---|---|---|
| `alice` | operator、approver、buyer、admin | 第一次体验完整闭环，权限最全。 |
| `bob` | operator | 测试普通操作员，不能审批、下单或管理故障模式。 |
| `carol` | approver | 测试审批箱。 |
| `dave` | buyer | 测试建单、下单、收货和关闭。 |
| `eve` | admin | 测试管理页面和模拟供应商故障模式。 |

不要在浏览器中选择 `system`。它只供后台任务使用，不能被冒充为人工操作人。

## 4. 第一次完整闭环（推荐照此顺序）

### 4.1 发起补货

1. 选择操作人 `alice`。
2. 打开“补货助手”。
3. 输入：`请检查华东仓紧固件分类，按14天规划窗口生成补货建议。`
4. 如果 Agent 追问仓库、产品或窗口，按页面提示补齐。规划窗口只允许 `7`、`14`、`30` 天。
5. 等待证据和计算结果显示。确认能看到库存、需求、规则、预测、补货公式和引用信息。
6. 点击生成草稿或等待流程进入待审批。此时会话可能显示 `interrupt`，这是等待人工审批，不是卡死。

如果只想测试单个 SKU，可输入：`检查华东仓 SKU-FASTENER-001，按7天窗口生成补货建议。`

如果故意不写仓库或窗口，可以观察“需要补充参数”状态；不要用猜测值替代缺失参数。

### 4.2 在审批箱处理计划

1. 打开“审批箱”。
2. 选择状态为待审批的计划。
3. 逐行阅读建议数量、供应商、规则证据和计算摘要。
4. 不需要的行选择“排除”，确认需要的行选择“批准”。
5. 提交审批。

如果页面返回 `PLAN_STALE`，说明库存、需求或规则在生成计划后发生了变化。回到补货助手重新生成计划，不要强行重复提交旧计划。

### 4.3 创建采购单

1. 打开“采购单”。
2. 选中刚刚批准的计划。
3. 点击“按供应商创建采购单”。系统会自动拆分供应商。
4. 记下采购单号和供应商。相同计划明细不能重复建单。

### 4.4 下达采购单

1. 保持操作人为 `alice`，或切换为 `dave`。
2. 在采购单列表找到状态 `po_created` 的采购单。
3. 点击“下达”。
4. 正常情况下状态变为 `ordered`。

如果模拟供应商超时，状态会变成 `order_unknown`。这时只能点击“查询恢复”，不能换一个幂等键盲目重下。查询得到供应商结果后，状态才会变为 `ordered` 或明确失败。

### 4.5 分批收货并关闭

1. 在 `ordered` 或 `partially_received` 的采购单上点击“收货”。
2. 输入本次实际到货量和唯一的收货事件号 `receipt_event_id`。
3. 需要分批到货时重复收货；总量不得超过采购单剩余量。
4. 发生网络重试时必须复用同一个 `receipt_event_id`，重复提交不会重复增加库存。
5. 所有明细收齐后点击“关闭”。未收齐、已取消或状态不允许时，关闭会被拒绝。

完成后可到“执行记录”和审计接口查看状态、请求 ID、操作人和结果。

### 4.6 如果定时任务抢先生成了计划

默认种子任务是每分钟执行一次。它可能在你打开页面前已经创建了活动建议，之后同一仓库/SKU 会被防重规则阻断。这不是应该靠重复点击解决的。

要做一次干净的手工演示，可以暂时停掉后台任务、重建合成数据，再按第 4 节操作：

```powershell
Set-Location 'D:\workplace\PyCharmMiscProject\仓储'
docker compose stop beat worker
docker compose exec api python -m app.seed.seed --reset
```

种子命令会清除当前演示业务数据并重建固定场景；不要在需要保留当前计划/采购单时执行。手工闭环完成后恢复后台任务：

```powershell
docker compose start worker beat
```

如果你不想重置数据，就直接使用已有的非空计划或采购单，并在“执行记录”中查看当前扫描为何被阻断。

## 5. 定时扫描闭环

1. 打开“定时任务”。
2. 查看已有任务的仓库、默认窗口和启用状态。
3. 点击“立即执行”进行一次手动扫描，或等待 Celery Beat 按配置触发。
4. 打开“执行记录”，选择最新执行记录。
5. 查看 `scanned`、`drafts`、`success`、`blocked`、`failed` 数量和逐 SKU 明细。

定时任务不是多轮对话。缺数据的 SKU 会单独标记 `blocked` 并告警，其他 SKU 继续扫描。单个 SKU 失败不应阻断整批任务。

## 6. 规则知识库和检索

1. 打开“规则知识库”。
2. 先用 `安全库存 华东仓` 或 `紧固件 交期` 搜索。
3. 查看命中的文档、规则优先级、版本、生效时间和引用片段。
4. 向量模型可用时会显示向量/混合检索；模型不可用时会降级到关键词检索并记录告警。

### 6.1 把自己的资料加入 RAG

当前 V1 已支持“管理员导入本地证据资料”。支持 Markdown 或纯文本，导入后会自动按段落切成检索片段，并建立关键词索引；Embedding 模型可用时同时建立向量索引。

按下面步骤操作：

1. 在右上角操作人切换器选择 `alice` 或 `eve`（必须包含 `admin` 角色）。`bob`、`carol`、`dave` 只能检索，不能导入。
2. 打开“规则知识库”，确认页面显示“导入本地资料”区域。
3. 在“资料标题”填写来源清楚的名称，例如“华东仓紧固件到货异常 SOP 2026-09”。不要把 API Key、密码、Cookie、客户隐私或未经授权的文件粘贴进来。
4. 选择资料类型：补货策略、安全库存、仓库作业、供应商约束或收货 SOP。
5. 将资料正文粘贴到文本框。建议保留原文标题、条款编号、单位、适用范围、版本和生效日期；单篇最多 80,000 个字符。
6. 点击“导入并建立检索索引”，等待页面显示“资料导入完成”。页面会显示片段数量，以及“向量 + 关键词”或“关键词”索引状态。
7. 在检索框输入资料中的两个或三个明确关键词并点击“检索”，确认结果显示你的标题和真实片段。检索结果中的来源片段才可以被 Agent 引用。
8. 回到“补货助手”，提出包含仓库、SKU/分类和 7/14/30 天窗口的请求。Agent 会把资料作为证据检索，但补货数量仍由结构化规则和确定性计算服务决定。

这项导入是“证据导入”，不是“规则发布”：文档中写着“安全库存=100”并不会自动把系统安全库存改成 100，也不会绕过规则冲突、权限、版本和审批。要让数字进入计算，必须由有权限人员通过后续规则管理流程形成经过校验的结构化规则；V1 暂不提供规则编辑、启停和回滚。相同资料重复提交时会复用已有索引，不会生成重复文档。

如果页面提示“角色不足”，先切换到管理员；如果显示“关键词”而不是“向量 + 关键词”，说明本机没有下载 Embedding 模型，RAG 仍然可用，只是走关键词检索。

如果要清空并重新生成演示数据和向量索引：

```powershell
docker compose exec api python -m app.seed.seed --reset --with-embeddings
```

这条命令会重建合成业务数据；它不是“只补索引”。确认不需要保留当前演示状态后再执行。

如果只想给现有数据补建向量索引、保留当前业务状态，使用：

```powershell
docker compose exec api python -m app.seed.seed --with-embeddings
```

## 7. 故障模式演示

故障模式只能由 `admin` 在管理页面或管理接口切换。可用值：

- `normal`：正常返回。
- `explicit_failure`：供应商明确失败，采购单应记录失败结果。
- `timeout_but_created`：供应商已创建但响应超时，系统应进入 `order_unknown`，只能查询恢复。

演示完毕后务必切回 `normal`。不要让故障模式影响下一次正常测试。若用 API 调试，接口为：

```text
PUT /api/v1/admin/fault-modes/{supplier_id}
GET /api/v1/fault-modes
```

请求头中需要带当前操作人对应的 `X-Actor-Id`。

## 8. 重置、停止和恢复

普通停止（保留数据库卷）：

```powershell
docker compose down
```

重新启动：

```powershell
docker compose up -d
```

只重启某个服务：

```powershell
docker compose restart api
docker compose restart worker beat
```

重建演示数据（会清除当前业务数据，必须显式使用 `--reset`）：

```powershell
docker compose exec api python -m app.seed.seed --reset
```

只有在明确要从零开始时才使用以下命令；它会删除 Docker 数据卷中的数据库和 Redis 数据：

```powershell
docker compose down -v
```

## 9. 常见状态和错误怎么理解

| 状态/错误 | 含义 | 正确动作 |
|---|---|---|
| `需要补充参数` | 仓库、SKU 或窗口不完整 | 按页面列出的字段补齐。 |
| `blocked` | 该 SKU 缺少数据、规则、供应商或覆盖条件 | 查看阻断码和下一步，不要编造数据。 |
| `PLAN_STALE` | 计划证据已变化 | 重新生成，不重复提交旧计划。 |
| `po_created` | 采购单已持久化，尚未下达 | 由采购员下达。 |
| `order_unknown` | 外部下单结果不确定 | 只调用查询恢复。 |
| `IDEMPOTENCY_KEY_REUSED` | 同一幂等键对应了不同请求内容 | 为新请求生成新键；同一请求重试必须复用原键。 |
| `RESOURCE_BUSY` | 同一资源正在被另一操作占用 | 等待后用同一操作键重试。 |
| `403` | 当前操作人没有角色权限 | 切换正确角色；不要绕过权限。 |
| `409` | 状态冲突、计划过期或重复操作 | 读取最新资源状态后按页面建议恢复。 |

## 10. 找 bug 的正确流程

### 10.1 先记录，再修改

每次发现问题，先保存以下信息：

```text
发现时间：
环境：Windows / Docker Desktop 版本 / 浏览器
操作人：alice/bob/carol/dave/eve
前置数据：是否刚重置种子，仓库、SKU、供应商、计划号、采购单号
复现步骤：按顺序写每一次点击和输入
预期结果：
实际结果：
页面错误码和提示：
请求 ID / operation_id / plan_id / po_id：
相关截图：注意遮挡 API Key、Cookie、Authorization
```

### 10.2 用最小步骤重现

1. 先确认 `docker compose ps` 和服务健康状态。
2. 必要时执行种子重建，记录重建前后状态。
3. 只保留能触发问题的最短操作序列。
4. 查看对应服务日志：

```powershell
docker compose logs --tail=200 api
docker compose logs --tail=200 worker
docker compose logs --tail=200 mock-supplier
```

5. 先判断问题属于页面、API、领域计算、数据库事务、后台任务还是外部供应商模拟器。

不要直接修改数据库绕过问题；那会破坏审计链，也可能把真正的状态机 bug 隐藏掉。

## 11. 如何补测试用例

原则是“先写会失败的回归测试，再改代码”。根据问题类型选择测试层：

| 问题类型 | 放置位置 | 例子 |
|---|---|---|
| 纯函数、公式、错误提示映射 | `backend/tests/unit/` | 补货量取整、阻断码对应下一步。 |
| 幂等、边界、随机组合 | `backend/tests/property/` | 重复收货不增加库存、同键异载荷无副作用。 |
| 多表事务、锁、状态迁移 | `backend/tests/integration/` | PLAN_STALE、按供应商拆单、超量收货回滚。 |
| REST 权限、错误码、SSE | `backend/tests/integration/` | bob 不能审批，错误响应包含 request_id。 |
| 多页面真实用户流程 | `backend/tests/e2e/` | 助手→审批→建单→下单→收货→关闭。 |

### 11.1 最小测试模板

新增测试时至少说明输入、预期状态和安全不变量。示意：

```python
def test_duplicate_receipt_event_does_not_increase_inventory(client, seeded_po):
    payload = {
        "quantity": 2,
        "receipt_event_id": "receipt-regression-001",
    }

    first = client.post(
        f"/api/v1/purchase-order-lines/{seeded_po.line_id}/receive",
        json=payload,
    )
    second = client.post(
        f"/api/v1/purchase-order-lines/{seeded_po.line_id}/receive",
        json=payload,
    )

    assert first.status_code in (200, 201)
    assert second.status_code in (200, 201)
    assert get_on_hand(seeded_po) == seeded_po.initial_on_hand + 2
```

实际项目中应复用现有 fixture、认证头和领域工厂，不要在每个测试里复制整套数据库初始化。

### 11.2 本地测试阶梯

先跑快的，再跑慢的：

```powershell
Set-Location 'D:\workplace\PyCharmMiscProject\仓储\backend'
pytest -q tests/unit
pytest -q tests/property
pytest -q tests/integration
pytest -q -m "not e2e"
pytest -q -m e2e
ruff check app tests
ruff format --check app tests
mypy app
```

前端改动后：

```powershell
Set-Location 'D:\workplace\PyCharmMiscProject\仓储\frontend'
npm run build
```

每个 bug 修复都至少加入一个回归测试；涉及安全不变量的改动，还要重新运行全部相关性质/集成测试。

## 12. 后续优化顺序

### P0：先提升可用性和可诊断性

- 每个失败状态都显示错误码、下一步和 request ID。
- 列表页增加筛选、分页、按状态排序和最近更新时间。
- 把“旧计划”“订单未知”“后台扫描失败”做成可追踪告警。
- 将启动检查、种子重建和测试命令写入 CI，并在提交前自动执行。

### P1：再提高数据和模型质量

- 扩充黄金集，分别评估 OFFLINE 和真实 LLM；不要只看一个“准确率”。
- 对参数字段、必要澄清、工具调用、RAG 引用、任务完成率、MAE/WAPE、P50/P95 和 Token 分开记录。
- 配置 Langfuse 后验证云端 trace、脱敏和失败不中断业务。
- 逐步接入真实业务格式的脱敏样本，但仍标明样本来源和限制。

### P2：最后做 V1.1/V2 能力

公网部署、JWT/OAuth、真实 WMS/ERP、金额阈值审批、预算/供应商优化和动态安全库存属于后续版本。它们会改变权限、数据模型和验收范围，不应在 V1 bug 尚未收口时一起引入。

## 13. 给后续编码 Agent 的一次性工作提示词

将下面提示词粘贴给负责下一轮开发的 Agent。它已经包含本项目的边界，不需要反复让 Agent 重新规划 Docker：

```text
你正在维护 StockMind V1。先读取并遵守：
1. AGENTS.md；
2. StockMind需求规格说明书.md；
3. StockMind架构设计文档.md；
4. StockMind架构决策记录ADR001.md；
5. StockMind项目说明.md；
6. StockMind使用说明书.md。

当前目标：做一次可复现的 V1 使用验收、找 bug，并把发现的问题转化为回归测试和最小修复。

约束：
- 先检查当前工作区、容器状态和已有未提交改动，绝不覆盖用户改动。
- 不把合成数据、OFFLINE 结果或未执行的指标写成真实业务结论。
- 不扩大到 V1.1/V2；不新增未约定的架构组件。
- Agent 只能生成补货草稿；审批、建单、下单、收货、关闭、故障模式切换必须保留角色和状态校验。
- 保持越权、重复采购、重复入库、未知订单盲目重试、非法状态迁移、幂等键异载荷副作用为零。

执行顺序：
1. 运行 docker compose ps，并访问前端、API /health 和模拟供应商 /fault-modes；失败先定位，不伪造“已通过”。
2. 按《StockMind使用说明书》第4节走一遍助手→审批→建单→下单→分批收货→关闭；再走第5节定时扫描和第7节故障模式。
3. 为每个问题记录：复现步骤、预期/实际、操作人、资源 ID、错误码、request_id、日志位置。
4. 先新增一个最小失败回归测试，再修改实现。按问题类型放入 unit/property/integration/e2e 正确目录。
5. 运行受影响测试，再运行 pytest -q -m "not e2e"、ruff、format、mypy；涉及页面则运行 npm run build，涉及闭环则运行 e2e。
6. 只修复证据支持的问题；若无法重现，说明已做的检查和下一步，不要猜测性重构。

最终报告必须包含：
- 实际执行的命令和通过/失败结果；
- 每个 bug 的根因、修复文件、回归测试；
- 安全不变量专项结果；
- 未完成项及真实阻塞原因；
- 仍在运行的服务地址；
- 不要输出任何 API Key、Cookie 或完整 Authorization。
```

## 14. 交付前检查清单

- [ ] Docker Desktop 正常，`docker compose ps` 无异常重启。
- [ ] 前端可打开，能看到当前操作人和 offline/LLM 状态。
- [ ] 至少完成一次助手到关闭的完整闭环。
- [ ] 至少验证一次缺参、一次 `PLAN_STALE`、一次 `order_unknown` 查询恢复。
- [ ] 定时扫描执行记录能看到逐 SKU 结果。
- [ ] 发现的每个 bug 都有回归测试。
- [ ] `pytest`、Ruff、格式检查、Mypy 和前端构建结果已记录。
- [ ] 报告中没有把演示数据、预期指标或未验证能力写成事实。
