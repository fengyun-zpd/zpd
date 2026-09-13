# StockMind 智能仓储补货 Agent - 需求规格说明书

> **版本**：V4.8
> **修订日期**：2026-09-13
> **状态**：V1 完整验收版（冻结基线）已实现并本机运行验证；V1.1 扩展能力已实现（代码 + 测试），验证边界见修订记录
> **首版范围**：V1 本机 Docker Compose 完整演示
> **数据声明**：全部为固定随机种子生成的合成数据

## 0. 产品定位

StockMind 面向中小制造/批发仓库，解决“低库存发现晚、补货依据不可追溯、下单副作用无人把关”问题。它不是完整 WMS，也不是让 LLM 自由执行采购的聊天机器人，而是一个具备工具编排、检索证据、确定性计算、人工审批和异常恢复能力的 Agent 应用。

## 1. 用户与角色

V1 不实现真实认证。接口通过 `X-Actor-Id` 选择演示用户，后端从种子用户表查询角色，不信任前端直接提交的角色字符串。创建会话时请求体不得改变身份；读取或发送会话必须校验 `thread_id` 属于当前 `X-Actor-Id`。

| 角色 | 允许动作 |
|---|---|
| `operator` | 发起补货、查看计划、登记到货 |
| `approver` | 查看审批证据、逐条批准/排除、驳回计划 |
| `buyer` | 创建采购单、下达、手动查询未知订单、确认关闭、取消尚未下达的采购单 |
| `system` | 执行定时扫描、后台查询未知订单，生成草稿、执行记录和告警 |
| `admin` | 配置/立即执行/重跑定时任务、查看所有运行记录、切换模拟供应商故障、导入本地知识资料；可执行采购单下达、未知订单查询、收货、关闭和取消 |

同一演示用户可以配置多个角色用于本机演示，但每个 API 操作仍必须进行后端权限校验。JWT/OAuth 放 V1.1。

`system` 是 Celery Worker 使用的内部服务主体，不接受浏览器或普通 REST 请求提交的 `actor_id` 冒充。管理员触发立即执行或重跑时，执行主体记为 `system`，同时在执行记录中保存 `triggered_by_actor_id`。

## 2. 业务触发

### 2.1 手动触发

操作员在补货助手中输入自然语言，例如“帮我检查华东仓未来两周需要补货的紧固件”。Agent 解析出仓库、SKU/商品范围和规划窗口；缺少必要参数时必须追问。用户可修改解析结果后再确认。

### 2.2 定时触发

Celery Beat 按 Web 中配置的周期调用扫描服务。定时配置必须保存 IANA 时区；V1 种子仓库统一使用 `Asia/Shanghai`，最低周期为 1 分钟以便演示，生产建议每日执行，默认规划窗口为 14 天。每次执行都写入执行记录：触发方式、计划时间及时区、开始/结束时间、扫描量、生成草稿数、成功/失败数、错误摘要和关联 trace。

定时任务不进入 LangGraph 对话且无法追问。单个 SKU 缺少库存、适用规则或供应商关系，或发生规则冲突时，execution_item 标记为 `blocked`；未分类的运行异常标记为 `failed`。两者都产生或刷新页面告警，其他 SKU 必须继续处理。每个 SKU 使用独立事务或保存点，单行失败不得回滚整个批次。

同一 `schedule_id + scheduled_at` 只能创建一个计划执行；立即执行和手动重跑必须由管理页面生成并复用请求幂等键。每个扫描对象写入 `execution_item`，保存仓库/SKU、结果、错误码、计划明细/告警引用和 trace。若一次扫描没有任何 `valid` 明细，不创建空的待审批计划，只保存 execution、execution_item 和告警。

## 3. 对话与 Agent 要求

系统使用一个“补货助手 Agent”，由 LangGraph 管理会话状态和工具编排，不宣传多 Agent 协作。

### 3.1 必要参数

- 仓库；
- SKU 或商品范围；
- 规划窗口，只允许 `7/14/30` 天。

紧急程度、输出格式等属于非必要信息。用户可以关闭非必要建议，但必要参数不全时禁止生成补货草稿。Agent 不得猜测仓库、SKU、规划窗口或业务规则；计划起始日由领域服务按仓库时区取当前业务日，不由 LLM 推断。

### 3.2 允许的 Agent 行为

- 意图分类和参数提取；
- 缺参追问与会话恢复；
- 调用库存、历史需求、在途、供应商和规则检索等只读工具；
- 调用“生成补货草稿”工具，结果必须通过领域校验；
- 引用规则证据并解释计算结果；
- 手动会话在草稿由领域服务落库并提交待审批后调用 LangGraph `interrupt`；授权审批人通过页面/REST 完成审批并写入业务数据库与审计日志后，`resume` 只读取带版本号的审批结果并恢复会话。

LLM 不直接写库存、不计算数字、不迁移采购状态，也不能调用审批、创建采购单、下单、未知订单恢复、收货、关闭/取消、修改规则和切换模拟供应商故障等副作用动作。后述动作只能由对应角色通过 REST API、页面或受控后台任务明确触发。checkpoint 只保存会话恢复状态，不是业务状态、权限或审批结果的事实源。

审批、驳回或替代待审批手动计划的事务同时写入持久化 `workflow_resume` 请求，提交后由 Celery 异步触发 `resume`，避免业务决定已提交但任务消息丢失。`resume` 失败、重复或 checkpoint 丢失不得回滚或重复业务决定；系统记录告警并允许用同一 `thread_id + plan_id + decision_version` 重试恢复。定时生成的计划没有会话 checkpoint，不写恢复请求。

## 4. 数据与规则需求

### 4.1 领域实体

文档按领域列出实体，不在代码完成前承诺固定表数量：

- 库存域：商品、仓库、库位、批次、库存量、库存移动；
- 补货域：补货计划、计划明细、结构化规则；
- 采购域：供应商、SKU-供应商关系、采购单、采购单明细、下单尝试记录；
- Agent 域：会话、消息、LangGraph checkpoint、持久化 workflow resume 请求；
- 知识库域：文档、分块、Embedding 和来源关联；
- 治理域：演示用户、审计日志、幂等操作记录、定时任务配置、执行记录、执行明细、告警。

### 4.2 合成数据

初始化脚本使用固定随机种子生成虚构数据：仓库、SKU、供应商、SKU-供应商关系、采购价、交期、历史出库和规则文档。历史出库覆盖 90-180 天，并植入库存充足、在途抵扣、无供应商、规则冲突、交期异常等场景。数据可一键销毁并重建，README 明确其不代表真实经营结果。

### 4.3 知识文档与结构化规则

V1 预置五类文档并由脚本入库：补货策略、安全库存、仓库特殊规则、供应商采购约束、到货异常 SOP。文档保存版本、生效时间、适用范围、启用状态和可引用分块。

RAG 只产生候选来源：向量召回与 PostgreSQL 全文检索通过 RRF 融合。规则解析器根据 `source_document_id`、`source_chunk_id`、版本、生效时间、仓库和 SKU 校验适用性，并把数字保存为结构化规则。运行时 LLM 不得从自然语言临时转出计算参数。

`admin` 可通过页面/API 导入 Markdown 或 TXT 形式的本地知识资料（单篇上限 80,000 字符）。系统按段落分块，建立关键词索引，并在 Embedding 可用时建立向量索引；导入请求必须携带幂等键并写入审计记录。导入资料仅作为不可信检索证据，不能自动创建、修改、启用或覆盖结构化规则，也不能把自然语言数字直接带入计算。

规则冲突采用确定性优先级：

```text
SKU 专属规则 > 商品分类规则 > 仓库规则 > 全局规则
```

同级按“已启用、当前有效、版本号最高”选择；仍冲突则阻断相关明细并进入人工处理，不猜测。

检索文本一律视为不可信数据。文档分块中的“忽略指令”、角色声明、系统提示、工具调用或外部链接不得改变 Agent 行为、权限和工具白名单；只有结构化规则服务输出的已校验字段可以进入计算。黄金集必须包含文档提示注入与伪造引用样例。

## 5. 预测与补货验收要求

### 5.1 预测

- 日需求序列按仓库时区聚合到完整业务日。来源确认完整但无出库的日期补 0；来源覆盖不明或缺失的日期不能补 0；
- 加权移动平均使用预测日前最近 28 个完整日，权重按时间从 1 到 28 递增，日均预测为加权和除以 `1+...+28`；
- 简单指数平滑使用与加权移动平均相同的最近 28 日训练窗口，固定 `alpha=0.3`，以窗口最早日需求初始化，按 `S_t = alpha * y_t + (1-alpha) * S_(t-1)` 更新；
- 用最近 28 个完整日做滚动起点、单步预测回测；每个验证日只使用其此前 28 日，首个验证日之前至少要有 28 个连续完整日，因此模型比较至少需要 56 日。`MAE = mean(abs(actual-forecast))`，`WAPE = sum(abs(actual-forecast)) / sum(actual)`；
- 验证期实际需求总和大于 0 时，按 `(WAPE, MAE, algorithm_id)` 升序选择；总和为 0 时 WAPE 记为 `null`，按 `(MAE, algorithm_id)` 选择。固定算法 id 顺序为 `wma_28_v1 < ses_a03_v1`；
- 少于 56 个连续完整日或覆盖状态不可信时，不生成销量预测值，标记 `fixed_safety_stock_fallback`，令 `coverage_demand=0` 并只使用固定安全库存计算；若固定安全库存规则也缺失则 `blocked`；
- 预测结果保存算法版本和输入快照，可回放；
- 不使用 LLM 预测销量。

### 5.2 供应商选择与拆单

同一 SKU 多供应商先排除禁用、超出适用期、缺少有效价格/交期/最小量/整箱倍数或不可供货的关系，再按“业务优先级升序 → 交期升序 → 采购价升序 → `supplier_id` 字典序升序”选择。V1 种子采购价统一为 CNY，不进行跨币种比较。无候选供应商时明细 `blocked`。Agent 只能解释结果。

一次计划允许多个 SKU。审批人逐条勾选批准、排除或整单驳回；未勾选明细标记 `excluded` 并保存原因。批准的明细按供应商聚合为多张采购单，供应商 A/B 不得混单。

### 5.3 补货量

```text
available = on_hand - reserved
planning_window = max(requested_window, lead_days + review_period_days)
coverage_demand = daily_forecast * planning_window
target_stock = coverage_demand + fixed_safety_stock
inbound = po_created/ordering/ordered/order_unknown/partially_received 的剩余未收量
net_demand = max(0, target_stock - available - inbound)
order_qty = 0                                      if net_demand == 0
order_qty = ceil(max(net_demand, minimum_order_qty) / pack_multiple)
            * pack_multiple                       if net_demand > 0
```

供应商必须先于补货量计算选定，`lead_days`、`minimum_order_qty` 和 `pack_multiple` 均取自选中的 SKU-供应商关系。`on_hand`、`reserved`、交期、复查周期、安全库存、最小采购量和整箱倍数必须为合法非负值，并满足 `reserved <= on_hand`、`pack_multiple > 0`；超过业务数量上限也必须拒绝。非法输入标记 `blocked`，不得静默修正。

所有中间量、规则来源、输入快照、选中供应商关系和算法版本必须写入补货计划明细，并对仓库业务日、库存/预留版本、采购承诺、需求截止日、规则版本、供应商关系版本和算法版本生成 `decision_input_hash`。降级模式下 `daily_forecast`、MAE 和 WAPE 保存为 `null`，同时保存降级原因，不能用 0 冒充预测结果。

`po_created` 表示已经形成内部采购承诺，`ordering` 和 `order_unknown` 表示不能安全地假定外部订单不存在，因此都必须计入有效在途量。只有采购单被授权取消、查询确认外部未创建，或剩余量为零时，才从有效在途量中移除。

### 5.3.1 数值与哈希规范

- V1 库存、采购和收货数量使用 SKU 基本单位的非负整数；预测、中间指标使用十进制数，禁止二进制浮点直接进入持久化业务计算；
- 确定性服务使用十进制有效精度 28，计算中间过程不提前取整；计算值持久化为 12 位小数并采用 `ROUND_HALF_UP`，页面与评测展示统一为 6 位，最终订货量只按上述 `ceil` 公式取整；
- V1 金额统一为 CNY，单价保留 2 位小数；不实现计量单位换算、跨币种或阶梯价格；
- 载荷哈希和 `decision_input_hash` 使用带 `schema_version` 的 UTF-8 canonical JSON：对象键按字典序排列，集合按稳定业务 id 排序，日期/时间使用 ISO 8601 和明确时区，十进制数使用无指数规范字符串，`null` 显式保留；哈希算法为 SHA-256；
- 哈希规范版本必须随计划、幂等记录和下单尝试保存；规范升级不得重写历史哈希。

## 6. 状态与操作

### 6.1 补货计划

```text
draft → pending_approval → approved/rejected/superseded
approved → superseded（仅尚未创建任何采购单明细时）
```

计划明细可附加 `valid`、`excluded`、`blocked` 标记。`rejected` 和 `superseded` 为终态。重新计算必须新建修订版本并通过 `revision_of_plan_id` 关联原计划；原计划只有在尚未关联任何采购单明细时才能被修订替代。

若审批人排除后没有剩余有效明细，计划只能转为 `rejected`，不能生成空的 `approved` 计划。创建修订版时，旧活动明细的释放与新明细的创建必须在同一事务中完成。

审批和创建采购单都必须在事务中重新读取当前输入并计算 `decision_input_hash`。任一拟批准明细在审批时发生变化，或任一已批准明细在建单时发生变化，整次命令返回 HTTP 409 `PLAN_STALE` 和变化字段，不审批、不建任何采购单，也不静默重算数量；审批人可以明确排除已变化明细后重新提交审批。需要重算时，操作员通过受控草稿工具创建整单修订版；修订创建成功后原计划转为 `superseded`。创建采购单命令对所有供应商拆单全有或全无。

### 6.1.1 活动建议防重生命周期

- 同一仓库/SKU 同时最多存在一个 `active_for_dedupe=true` 的有效计划明细，不以规划窗口不同为由并行创建第二条未落实建议；
- 有效明细创建时设为活动；被排除、整单驳回或由修订版替代时，在同一事务中解除活动状态；
- 创建采购单时，批准明细与采购单明细必须在同一事务中建立唯一关联并解除活动状态；采购单随后通过有效在途量参与下一次计算；
- 批准但尚未创建采购单的明细继续保持活动，防止审批后、建单前重复建议；
- `blocked` 明细不占用活动建议唯一约束。同一仓库/SKU/阻断码只保留一个活动告警；条件恢复后关闭告警，重新计算后方可生成有效建议；
- `active_for_dedupe` 只能由领域状态迁移维护，API 和 LLM 不得直接修改。

数据库使用部分唯一索引 `UNIQUE (warehouse_id, product_id) WHERE active_for_dedupe`。并发创建命中该约束时，服务返回 HTTP 409 `ACTIVE_REPLENISHMENT_EXISTS` 和已有计划/明细 id，不得暴露数据库异常或创建第二条建议。采购单明细对 `plan_line_id` 建立唯一约束，保证一个批准明细最多落实一次。

### 6.2 采购单

```text
po_created → ordering | cancelled
ordering → ordered | po_created | order_unknown
order_unknown → ordered | po_created
ordered → partially_received | received
partially_received → partially_received | received
received → closed
```

允许转换及守卫条件如下：

| 当前状态 | 目标状态 | 条件 |
|---|---|---|
| `po_created` | `ordering` | `buyer` 发起新的下单尝试 |
| `po_created` | `cancelled` | 尚无进行中/未知/成功的外部下单尝试 |
| `ordering` | `ordered` | 供应商明确返回成功和外部订单号 |
| `ordering` | `po_created` | 供应商明确返回未创建订单的失败 |
| `ordering` | `order_unknown` | 超时、断连或响应无法证明是否创建 |
| `order_unknown` | `ordered` | 查询确认外部订单已创建 |
| `order_unknown` | `po_created` | 查询确认外部订单不存在 |
| `ordered` | `partially_received` | 首次收货大于 0 且小于剩余量 |
| `ordered` | `received` | 首次收货等于剩余量 |
| `partially_received` | `partially_received` | 本次收货后仍有剩余量 |
| `partially_received` | `received` | 累计收货量等于采购量 |
| `received` | `closed` | `buyer` 明确确认关闭 |

`closed` 和 `cancelled` 为终态。V1 不提供已下达采购单的供应商取消接口，因此 `ordered`、`order_unknown` 和 `partially_received` 不得直接转为 `cancelled`。审批通过不自动创建或下达采购单；采购员必须先点击“创建采购单”，再点击“下达”。

每次从 `po_created` 进入 `ordering` 都创建不可变的下单尝试记录，包含尝试序号、内部命令幂等键、供应商幂等键、请求摘要哈希、结果、外部订单号和时间。网络重试必须复用当前尝试的供应商幂等键；只有明确失败，或 `order_unknown` 查询确认不存在并回到 `po_created` 后，采购员才能发起带新尝试记录的新下单命令。

供应商调用采用以下固定顺序，不尝试跨数据库与供应商 API 建立伪事务：

1. 在数据库事务中创建下单尝试、把采购单从 `po_created` 改为 `ordering` 并提交；
2. 使用该尝试的供应商幂等键调用外部 API；
3. 明确成功、明确失败或结果歧义分别按允许转换表写回；
4. Worker 崩溃或超过配置超时仍处于 `ordering` 时，后台先将其转为 `order_unknown`，再用供应商幂等键查询，禁止直接重发下单。

### 6.3 到货

- 每次收货带唯一 `receipt_event_id`；
- 重复事件不重复增加库存；
- 单次和累计收货量不能超过采购单剩余量；
- 部分到货为 `partially_received`，全部收齐为 `received`；
- `received` 后由采购员明确确认 `closed`。

收货事务必须锁定采购单明细并原子完成：写入收货事件、创建库存移动、更新库存量、累计收货量、采购单状态/版本和审计日志。任一步失败则全部回滚。相同 `receipt_event_id` 与相同规范化载荷返回原结果；相同事件 id 但仓库、采购单明细或数量不同，返回 HTTP 409 `RECEIPT_EVENT_REUSED`。

### 6.4 幂等与并发

所有状态变更 API 和后台命令必须提供幂等键；Agent 的草稿工具和定时扫描分别提供 `operation_id` 与 `execution_id + warehouse_id + product_id` 作为命令标识。服务端按 `(principal_id, command_type, aggregate_ref, idempotency_key)` 确定作用域，其中 `principal_id` 是用户或内部服务主体，更新命令的 `aggregate_ref` 是对象 id，创建命令则使用规范化业务目标键。记录保存规范化请求载荷哈希、操作状态、响应、关联对象和审计信息。

- 同一作用域、同一幂等键和相同载荷：已成功或终止失败时返回保存结果，不重复执行副作用；标记为 `retryable` 的前置失败可在同一操作记录上恢复；
- 同一作用域、同一幂等键但载荷不同：返回 HTTP 409 和稳定错误码 `IDEMPOTENCY_KEY_REUSED`；
- 操作仍在执行：返回 HTTP 202、稳定状态 `OPERATION_IN_PROGRESS` 和可查询的操作标识；
- 在取得业务锁或执行任何副作用前发生的瞬时失败可记为 `retryable`；同键重试继续该操作。已经产生外部不确定结果的操作不得标记为 `retryable`；
- V1 不自动删除幂等操作记录；后续如引入清理策略，保留期不得短于关联业务对象的审计期；
- 审批、排除/驳回、建单、下单、未知状态恢复、收货、关闭/取消及管理配置均执行角色校验、允许转换校验和乐观版本校验；
- `receipt_event_id` 在业务上全局唯一，独立于 HTTP 幂等键；同载荷重复事件返回原收货结果，异载荷复用必须拒绝；
- 外部下单沿用同一供应商幂等键。超时进入 `order_unknown` 后只能查询恢复，不能用新键重新下单。

非外部副作用命令由数据库事务原子完成。执行进程崩溃后，后台可在操作租约过期时以同一幂等记录恢复；涉及供应商的陈旧操作必须走下单尝试查询协议，不能仅凭 `idempotency_operation=in_progress` 重放外部请求。

草稿/修订、审批、创建采购单、取消和收货等影响补货判断的事务，必须按 `(warehouse_id, product_id)` 排序后取得 PostgreSQL 事务级 advisory lock，并在锁内完成版本/哈希校验和写入；统一排序避免多 SKU 死锁。锁超时返回 HTTP 409 `RESOURCE_BUSY`，调用方使用同一幂等键重试。Redis 锁只用于任务调度协调，不能替代数据库唯一约束、事务锁或版本校验。

乐观版本不匹配返回 HTTP 409 `VERSION_CONFLICT`，非法迁移返回 HTTP 409 `INVALID_STATE_TRANSITION`，角色不符返回 HTTP 403 `FORBIDDEN`。浏览器对一次用户动作生成一个 UUID 幂等键，并在网络重试时复用；用户再次明确发起动作时才生成新键。

## 7. 模拟供应商 API

模拟 API 支持正常成功、明确失败、超时但实际创建、相同幂等键重复请求、查询结果为已创建/不存在五类结果。明确失败回到 `po_created`；超时或歧义结果进入 `order_unknown`。系统查询后根据外部事实转为 `ordered` 或回到 `po_created`，禁止盲目重试。

故障模式只能由 `admin` 通过页面/REST 切换并写入审计日志，不能暴露为 Agent 工具。`buyer` 可以手动触发自己有权限的未知订单查询，`system` 可以执行后台恢复任务；两者必须调用同一状态恢复服务并遵守相同幂等与版本校验。

## 8. RAG 与 API 验收

### 8.1 RAG

- 本地中文 Embedding + pgvector；
- jieba 分词 + PostgreSQL 全文检索；
- 向量和关键词双路召回，RRF 融合；
- 引用 `[N]` 必须能映射到真实启用文档分块；
- 无可靠来源时拒答；
- `admin` 导入本地资料后可立即按关键词检索；重复提交同一内容不得产生重复文档；
- Cross-Encoder 重排放 V1.1。

### 8.2 主要 API

API 至少覆盖：会话流式对话、补货计划查询/审批、采购单创建/下达、供应商状态查询、分批收货/关闭、未下达采购单取消、定时任务配置/立即执行/重跑、执行记录/告警查询、规则只读查询、管理员知识资料导入和演示故障注入。所有请求返回 request/trace id，错误使用稳定错误码；状态变更请求还必须返回幂等操作标识和最新对象版本。会话流式对话为节点级真流式，事件带递增序号；客户端断线后携 `Last-Event-ID` 续传，只重放未收到的进度、不重新执行 Agent（避免重复生成草稿触发防重），缓冲缺失时从 checkpoint 恢复最终态。断线续传的 `Last-Event-ID` 分三态：`absent`（无请求头 = 正常新请求）、`valid`（进入续传）、`invalid`（**返回稳定 422**）；非法游标不得静默开启新一轮任务，续传不得重新执行 Agent 或重复调用 `generate_draft`，补流事件序号必须单调递增。

**V1.1 扩展 API（已实现，突破 V1 冻结基线）**：

- `POST /api/v1/agent/start`：以自然语言启动一轮 Agent，创建会话、持久化用户消息，并以 SSE 逐节点返回事件（`agent_start` / `node_end` / `tool_call` / `draft_created` / `interrupted` / `message` / `error` / `done`）；响应头必须包含 `X-Request-Id` 与 `X-Thread-Id`；`agent_start` 必须包含 `thread_id`。
- `POST /api/v1/agent/{thread_id}/resume`：澄清补充（`content`，同一会话继续且不新建 thread）或审批恢复（`plan_id` + `decision_version`）。审批恢复**只读取数据库已提交决定并恢复对话，不得执行审批、建单、下单或任何业务副作用**；恢复键严格为 `thread_id + plan_id + decision_version`，且必须**同时**满足：会话属于当前 actor（403）、`plan_id` 存在（404）、计划**绑定到当前 `thread_id`**（403，禁止跨会话恢复）、`decision_version` 与数据库当前版本一致（409）、计划已产生可恢复决定（`approved`/`rejected`/`superseded`，否则 409）、checkpoint 确实处于等待恢复（否则 409）。任一不满足都**不得进入** LangGraph resume、不得改变计划状态、不得新建采购单；两类参数同时提供或不提供均返回 422。
- `GET /api/v1/agent/{thread_id}/state`：返回安全状态摘要（`thread_id` / `intent` / `missing_params` / `offline` / `degradation_reason` / `step_count` / `outcome` / `plan_id` / `needs_approval` / `response` / `loop_blocked`），不得返回密钥、完整 Prompt 或检索正文。

**LLM 运行模式（V1.1）**：`LLM_MODE` 只允许 `auto` / `llm` / `offline`（容忍大小写与空白，空值按 `auto` 处理；其他取值在**配置加载时立即失败**）。`auto`（默认）在 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 三者齐备时优先真实 LLM，配置不完整或调用失败时回退 OFFLINE；`offline` 强制不调用外部模型；`llm` 用于评测真实模型路径，配置不完整时必须明确降级，不得假装调用成功。降级原因必须是稳定 code（`LLM_MODE_OFFLINE` / `LLM_NOT_CONFIGURED` / `LLM_TIMEOUT` / `LLM_INVALID_RESPONSE` / `LLM_UNAVAILABLE`），并在状态、SSE 事件、日志与评测报告中记录，禁止静默降级。

**Agent 失控防护（V1.1）**：每个节点执行递增 `step_count`，超过 `AGENT_MAX_STEPS` 必须进入 `escalate` 节点并返回稳定业务语言“任务步骤超过安全上限，已停止自动处理，请转人工处理。”，且不得继续调用只读工具或写工具；同一会话中「同一工具 + 同一规范化参数」超过 `AGENT_TOOL_DUPLICATE_LIMIT` 时必须写告警、置 `loop_blocked`、停止后续工具调用并转人工；不同参数不得被误判为重复；计数取自状态（checkpoint 保存），断线重连不得丢失。

**成本（V1.1）**：真实 LLM 调用记录 input/output/total token（不记录密钥与完整敏感 Prompt）；`LLM_PRICE_PER_1K_INPUT` / `LLM_PRICE_PER_1K_OUTPUT` 为**假设单价**，未配置时成本字段为 `null`（不写 0）；评测报告输出总成本、平均每任务成本、单价来源与是否假设单价，并声明“不是供应商真实账单，生产环境需用真实账单校准”；OFFLINE 模式不得产生 LLM 成本。

**Agent 决策链语义追踪（V1.1）**：每轮 trace 至少关联 `request_id`、`thread_id`、`plan_id`、节点执行顺序、选择的工具、工具参数摘要、工具结果规模与稳定错误码、RAG 引用的文档块 ID、最终 `outcome`、`degradation_reason`、`step_count`、`loop_blocked`；不得记录密钥、完整 Prompt、完整检索正文或敏感载荷；无检索命中时如实记录"无证据"，不得虚构 citation。

## 9. 前端验收

实现补货助手、补货工作台、审批箱、采购单、定时任务、执行记录、规则知识库和操作人切换器八个页面。采购单页仅在 `po_created` 状态显示取消命令。必须能从浏览器走完一条手动闭环，并能展示一次定时扫描、一次下单未知状态恢复和一次重复收货被幂等拦截。

规则知识库页面对所有角色提供检索与来源查看；仅 `admin` 可见本地资料导入表单。导入成功后必须展示资料标题、检索片段数量与“向量 + 关键词”或“关键词”索引状态，并明确提示“资料不会自动修改补货计算规则”。

## 10. 评测与工程验收

固定黄金集覆盖：正常补货、无需补货、全部 `po_created/ordering/ordered/order_unknown/partially_received` 状态的在途抵扣、最小量/整箱倍数、多供应商、无供应商、预测算法确定性、全零验证集、预测降级、无规则、规则冲突、缺参澄清、库外拒答、文档提示注入、伪造引用、审批前输入变化、审批后建单前输入变化、修订版原子替代、并发活动建议、空计划拦截、重复扫描/审批/建单/下单/收货、幂等键异载荷复用、收货事件异载荷复用、显式失败重试、陈旧 `ordering` 恢复、超时后外部已创建/不存在、非法状态迁移、浏览器冒充 `system`。

分别报告：参数字段准确率；Recall@k、MRR、引用正确率；算法示例/边界/性质测试；任务完成率、正确工具调用率、必要澄清率；MAE/WAPE；P50/P95、Token 和成本。安全不变量单独报告：越权操作成功数、重复有效建议数、重复采购数、重复入库数、未知状态盲目重试数、非法状态迁移成功数、幂等键异载荷产生副作用数均必须为 0。其他结果只填写实测数字。

**评测分表（V4.3）**：黄金集评测按 OFFLINE 确定性模式与真实 LLM 模式分表报告，报告包含样本量、并发度、机器、模型与运行时间戳；真实 LLM 与 OFFLINE 指标不混报；成本优先读取可配置单价（`LLM_PRICE_PER_1K_INPUT/OUTPUT`），缺少单价时只报告 Token；小样本延迟不构成容量结论；Langfuse 无凭证时性能与 Token 通过本地结构化记录采集，云端 trace 标记未验证。

测试工具：`pytest`、`Hypothesis`、`Playwright`、Ruff、Mypy 和 GitHub Actions。核心评测必须可由固定种子重跑，报告带用例哈希和语料指纹。CI 在无真实密钥环境运行：不配置 LLM/Langfuse 凭证，后端以 OFFLINE 模式测试；pgvector 扩展由 Alembic 迁移内 `CREATE EXTENSION IF NOT EXISTS vector` 保证（CI 的 PostgreSQL service 不挂载 db-init 目录）；依赖以 `requirements.lock` 精确约束；compose `env_file` 使用 `required: false`（干净 checkout 无 `.env` 仍可 config/build）；另含容器构建检查与密钥泄露扫描（只报文件名）。CI 云端成功运行需 push 后由 GitHub Actions 执行；workflow 文件存在不等于 CI 已通过，未提交仓库前无云端运行记录。

## 11. 非功能与诚实边界

- 默认实现与验收范围为 V1；只有用户明确指定时才设计 V1.1/V2 能力，且必须标注为规划中、未实现；
- V1：Docker Compose 本机启动；V1.1 再做公网部署；
- 数据库：PostgreSQL + pgvector；缓存/任务协调：Redis；
- 观测：Langfuse Cloud（可选，默认 no-op），输入输出脱敏；未配置凭证时安全 no-op，配置后记录对话/LLM/工具/RAG/领域计算与错误并脱敏；云端验证需真实凭证；
- 审计：操作者、业务动作、前后状态、错误码和关联对象，不记密钥和完整 Prompt；
- V1 已实现并本机运行验证：Docker Compose、领域服务/API、迁移与种子、Celery、LangGraph 对话、RAG、确定性计算、前端八页面与测试套件齐备；对话在无 LLM Key 时运行离线演示模式（页面标注 offline，不冒充真实 LLM），配置 LLM Key 时使用真实模型（已实测 DeepSeek 完成意图/参数提取与草稿生成）。评测状态：安全不变量 7 项专项检查全为 0；黄金集评测（固定种子可复现）已产出 OFFLINE 与真实 LLM 双模式分表报告——参数字段准确率 0.857、必要澄清率 1.0、RAG Recall@5=0.8 / MRR=0.8 / 引用正确率 1.0、MAE 1.32 / WAPE 0.14、OFFLINE 任务完成率 0.667 / 工具调用正确率 0.929、真实 LLM 任务完成率 0.667 / 工具调用正确率 0.929（真实 LLM 模式非确定性，小样本不构成容量结论）、P50/P95 实测、Token 实测（真实 LLM 3 例总量 859）；成本仅在有可配置单价时估算，未配置则只报告 Token；Langfuse 观测代码已实现、本地 mock 单测通过，云端未验证（未配置凭证）；
- 镜像交付：本仓库仅使用 CPU Embedding，后端镜像使用官方 CPU-only PyTorch（不安装 CUDA 运行时）；
- 不承诺完整 WMS、多租户、高并发、真实供应商接入或生产级认证。

### 11.1 后续业务插件需求（V2/V3，未实现）

新项目远程仓库为 <https://github.com/fengyun-zpd/dianshang-shouhou>。后续售后工单插件应复用 V1 的审计、幂等、RAG 证据和人工审批原则，并新增订单核验、物流调查、退款草稿、地址变更和工单关闭等能力。多 Agent、MCP、Graphiti/Neo4j、模型微调和 Mule Agent Bridge 只有在对应 ADR、测试和评测完成后才能进入实现范围；本节不是 V1 验收承诺。

## 12. 修订记录

- V4.11（2026-09-13）：发布前隔离验收补充——独立 Docker Compose 全新构建、迁移/种子初始化、关键后端回归和 9 条 Playwright 浏览器 E2E 全部通过并清理隔离环境；真实 MCP 宿主联调、真实 LLM 调用和真实账单成本仍未验证。

- V4.10（2026-09-13）：**V1.1 最终修订**——①`Last-Event-ID` 分三态（`absent`/`valid`/`invalid`），非法游标返回稳定 422，不得静默开启新任务；②`LLM_MODE` 白名单化（`auto`/`llm`/`offline`，非法值配置加载即失败）；③新增 **Agent 决策链语义追踪**要求（节点顺序 / 工具摘要 / 结果规模 / RAG 引用 / 降级原因 / 步数 / 门禁状态，且不得虚构 citation）；④修复测试夹具：显式注册 ORM 模型并同步 app 连接池，消除 `relation "warehouse" does not exist` 的环境竞态。V1 冻结基线不变。**验证边界**：DB 集成测试本轮已执行；容器化与真实 MCP 宿主联调未执行；成本为假设单价估算。

- V4.9（2026-09-13）：**V1.1 扩展收口（安全边界与状态恢复正确性）**——①审批恢复增加计划与会话绑定校验（会话归属 / 计划存在 / 计划绑定本会话 / 决定版本一致 / 计划已产生可恢复决定 / checkpoint 处于等待恢复），失败路径不进入 LangGraph resume、不改变计划状态、不新建采购单；②SSE 续传原子区分「缓冲无此 turn / 未完成 / 已完成」，`Last-Event-ID` 已指向 `done` 及其后不再补流，补流序号单调递增，越权 turn 返回稳定 `error` + `done`；③受控写工具 `generate_draft` 纳入与只读工具相同的重复调用门禁；④MCP 默认 actor 更正为业务库真实用户 id `bob`，输入边界进入客户端可见 `inputSchema`；⑤评测脚本 `MODE` 白名单化并强制设置 `LLM_MODE`。V1 冻结基线保持不变。**验证边界**：DB 集成测试本轮已执行；容器化部署与真实 MCP 宿主联调未执行；成本为假设单价估算。

- V4.8（2026-09-13）：**V1.1 扩展需求（已实现代码 + 测试，突破 V1 冻结）**——新增 `LLM_MODE` 三态与稳定降级原因要求（禁止静默降级）、HTTP Agent 主链路三个端点（`agent/start`、`agent/{thread_id}/resume`、`agent/{thread_id}/state`，resume 只读已提交决定、不执行审批或下单）、Agent 失控防护（`step_count` 递增 + `escalate` 转人工、`tool_call_counts` 按规范化参数去重）、成本字段要求（假设单价、未配置为 `null`、报告须声明"不是供应商真实账单"）。对应 ADR-007。**验证边界如实标注**：本机未运行 PostgreSQL/Docker，`db` 标记测试与容器 E2E 未执行；未真实调用 LLM，真实模型路径与真实 Token/成本未实测；上述能力不得计入 V1 已验收范围。

- V4.7（2026-09-08）：V1 冻结。现有功能、权限、状态机、数据模型和演示流程作为验收基线保持不变；新增业务需求必须进入 V1.1 或更高版本，安全、数据正确性、构建阻塞和文档勘误修复除外。

- V4.6（2026-09-07）：收紧会话身份边界：创建会话以 `X-Actor-Id` 为唯一身份事实源，拒绝请求体身份不一致；读取和发送消息校验会话归属，避免跨演示用户访问或写入会话；补充会话归属集成回归测试。

- V4.5（2026-09-07）：收紧权限边界：采购领域服务与 REST API 使用同一角色矩阵，`admin` 可执行采购下达、未知订单查询、收货、关闭和取消；下单审计记录真实操作者；新增 `/health` 能力标识与 `/ready` 就绪检查，避免 Agent 路由缺失时误报完整可用；补充纯管理员闭环回归验证。

- 模块化演进基线（2026-09-04）：登记新项目远程 `fengyun-zpd/dianshang-shouhou`，补充售后工单业务插件、多 Agent、MCP、RAG/记忆、模型微调、可靠性和交付门禁的规划入口；不改变 V1 验收范围。

- V4.4 知识资料导入（2026-09-02）：用户明确要求可操作 RAG 后，新增管理员本地 Markdown/TXT 证据资料导入。单篇上限 80,000 字符，按段落分块，默认关键词索引、Embedding 可用时附加向量索引；导入采用幂等键、内容去重和审计。资料只作为不可信检索证据，不自动产生或修改结构化计算规则；新增后端集成测试和浏览器导入/检索测试。V1.1 仍保留完整文档运营与结构化规则发布能力。
- V4.3 功能优化（2026-09-01）：发布收口第一轮用户功能优化——错误可见性验收要求明确：缺参列出需补充字段、阻断显示原因与下一步、PLAN_STALE 显示变化字段并提供排除/重算入口、order_unknown 仅查询恢复、幂等键区分原结果/异载荷冲突、页面区分 OFFLINE/真实 LLM/关键词 RAG/向量 RAG 状态；依赖可复现验收补充 `requirements-rag.lock`（base+rag 精确锁定，torch 固定官方 CPU 源 2.6.0+cpu、零 CUDA 依赖）；新增 6 项单元测试与 3 个 Playwright 场景；全套测试 122 项、7 项安全不变量全为 0。未改变领域公式、权限边界、状态机与数据库事实源。
- V4.3 审计收口（2026-09-01）：V1 发布候选审计与 CI 收口——CI 验收明确为"workflow 文件存在不等于 CI 已通过"：Alembic 迁移内 `CREATE EXTENSION IF NOT EXISTS vector` 使 pgvector 扩展在 Compose/CI/裸机三场景可靠（CI 的 PostgreSQL service 不挂载 db-init 目录）；compose `env_file` 使用 `required: false`（干净 CI 无 `.env` 可 config/build）；CI 密钥扫描只报文件名不泄露匹配内容；CI 步骤顺序修正（干净库迁移→种子幂等→pytest）与 `POSTGRES_DSN` 一致性；新增 `backend/requirements.lock` 依赖锁文件（pip-tools，Dockerfile/CI 以 `--constraint` 应用）；评测 OFFLINE 模式强制禁用 LLM（避免误用真实模型）。CI 云端成功运行需 push 后由 GitHub Actions 执行，仓库未提交故无云端运行记录（如实标注，不以本机等价验证冒充）。
- V4.3 发布基线（2026-09-01）：V1 完整验收版发布基线收口——评测从"尚未形成正式报告"升级为双模式分表（OFFLINE 确定性 + 真实 LLM，含 P50/P95、Token、成本规则）；观测要求明确为"Langfuse 可选：无凭证安全 no-op、有凭证记录对话/LLM/工具/RAG/领域计算与错误并脱敏，云端未验证时如实标注"；镜像交付要求明确 CPU-only PyTorch（本仓库仅 CPU Embedding，不装 CUDA 运行时）；隔离全新部署验证与 CI 无密钥可运行要求纳入验收范围。
- V4.2 收口（2026-09-01）：本机收口验收——Docker Compose 容器化启动已实测（7 服务 Up、容器内迁移/种子幂等可重复、api 重启可重复启动）；真实 LLM（DeepSeek）对话验证通过；安全不变量 7 项专项检查全为 0；Embedding 向量路径在容器内实测通过（pgvector 召回+关键词+RRF 融合）；建立可复现黄金集评测入口（参数准确率 0.857、RAG Recall@5=0.8/MRR=0.8/引用正确率 1.0、MAE/WAPE 实测）；修复 Agent 会话连接泄漏、pgvector 检索绑定、seed 后 alembic stamp、db 初始化扩展与 Dockerfile 构建优化。
- V4.2（2026-08-31）：V1 实现完成——补充实现说明（定时扫描按仓库各建一张待审批计划；种子"交期异常"场景落地为"禁用关系"以满足数据约束），修正"代码尚未实现"状态描述。
- V4.1（2026-08-31）：定义可复现预测和取整公式、供应商前置选择、数值/哈希规范、决策新鲜度、活动建议防重、人工审批恢复、采购/收货状态机、外部下单恢复、统一幂等和定时逐 SKU 隔离。
- V4.0（2026-08-31）：确认 V1 完整本机闭环需求。
