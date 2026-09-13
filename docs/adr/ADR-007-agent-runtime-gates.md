# ADR-007：Agent 运行时门禁、LLM 模式与 HTTP 主链路

## 状态

已接受（V1.1 扩展能力，2026-09-13）。V1 冻结基线（ADR 001 修订 1.8）不变。

## 背景

V1 发布基线把 Agent 定位为"离线优先确定性图 + 单 Agent + 人工审批"，并在冻结时明确
"新增业务能力须进入 V1.1 或更高版本"。Agent 应用开发岗位重点考察真实 LLM 调用、
可编程 HTTP 主链路、成本可观测性、失控防护与 MCP 协议适配，因此需要在 V1.1 范围内
补齐这些能力，同时不削弱 V1 的可靠性、审计与权限边界。

## 决策

### 决策 1：LLM_MODE 三态与稳定降级原因

新增 `LLM_MODE`（`auto` 默认 / `llm` / `offline`）：

- `auto`：`LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` 三者齐备时优先真实 LLM；
  缺配置或调用失败则回退 OFFLINE，并在状态、SSE 事件、日志与评测报告中记录稳定降级原因；
- `offline`：强制不调用外部模型（评测确定性基线与无 Key 环境）；
- `llm`：评测真实模型路径；配置不完整时记录 `LLM_NOT_CONFIGURED` 并降级，
  **不发起请求，也不假装调用成功**。

降级原因是稳定 code（不得用异常文本代替）：`LLM_MODE_OFFLINE`（主动选择）、
`LLM_NOT_CONFIGURED`、`LLM_TIMEOUT`、`LLM_INVALID_RESPONSE`、`LLM_UNAVAILABLE`。

`LLM_MODE` 是**白名单字段**：只允许 `auto` / `llm` / `offline`（容忍大小写与空白，空值按
`auto` 处理）；其他取值在**配置加载时立即失败**，不得静默按默认模式运行。

### 决策 2：HTTP Agent 主链路

新增三个入口，使 Agent 可被程序化驱动（不再只由对话页面驱动）：

- `POST /api/v1/agent/start`：创建会话 + 持久化用户消息 + SSE 逐节点事件流
  （`agent_start` / `node_end` / `tool_call` / `draft_created` / `interrupted` /
  `message` / `error` / `done`），响应头含 `X-Request-Id` 与 `X-Thread-Id`；
- `POST /api/v1/agent/{thread_id}/resume`：澄清补充（同一 thread 继续，不新建会话）
  或审批恢复（读取已提交决定 + `Command(resume=...)`）；
- `GET /api/v1/agent/{thread_id}/state`：返回安全状态摘要（intent / missing_params /
  offline / degradation_reason / step_count / outcome / plan_id / needs_approval /
  response / loop_blocked）。

约束：身份事实源始终是 `X-Actor-Id` 并校验会话归属；`resume` **不执行审批、下单或任何
业务副作用**，业务状态仍以业务数据库为唯一事实源，checkpoint 只用于流程恢复。

审批恢复（`plan_id` + `decision_version`）必须**同时**通过以下校验，恢复键严格为
`thread_id + plan_id + decision_version`：

1. 当前 `thread_id` 属于当前 actor（会话归属，403）；
2. `plan_id` 存在（404）；
3. 计划**绑定到当前 `thread_id`**（`plan.thread_id == thread_id`，403）——禁止用其他会话的
   `plan_id` 触发恢复；
4. `decision_version` 与数据库当前版本一致（409）；
5. 计划已产生可恢复的已提交决定（`approved` / `rejected` / `superseded`，否则 409）；
6. checkpoint 确实处于等待恢复（存在待执行节点且其 `plan_id` 与请求一致，否则 409）。

任一校验失败都**不进入** LangGraph resume、不改变计划状态、不新建采购单。

**SSE 游标语义**：`Last-Event-ID` 分三态——`absent`（无请求头 = 正常新请求）、`valid`
（进入断线续传）、`invalid`（**返回稳定 422**）。非法游标**不得静默开启新任务**：那会让
客户端以为在续传，实际重新执行一轮 Agent，并可能重复调用 `generate_draft` 产生重复副作用。

### 决策 3：Agent 失控门禁（步数与工具重复）

- `step_count`：每个节点执行时由节点守卫统一递增；超过 `AGENT_MAX_STEPS` 即进入
  `escalate` 节点，返回稳定业务语言"任务步骤超过安全上限，已停止自动处理，请转人工处理。"
  且**不再调用任何只读工具或 `generate_draft`**；
- `tool_call_counts`：按"工具名 + 规范化参数（canonical JSON，键升序+紧凑分隔符）"计数；
  同一会话中相同键超过 `AGENT_TOOL_DUPLICATE_LIMIT` 时写告警日志、置 `loop_blocked`、
  停止后续工具调用并转人工；**不同参数不会被误判为重复**；
- **受控写工具 `generate_draft` 与只读工具共用同一门禁**：key 同样为「工具名 + canonical
  JSON 参数」（`products` 排序，避免同一集合不同顺序被误判为不同请求）；超限时**不执行
  领域服务**（因此不产生新草稿 / 新计划）、置 `loop_blocked` 并转人工；状态中不残留任何
  看似成功的 `draft_result` / `plan_id`；
- 计数取自 state（由 checkpoint 保存），断线重连不丢失；每个新 turn 重置 `step_count`，
  工具计数跨 turn 累积（"同一会话中"语义）。

### 决策 4：成本估算与诚实边界

- 每次真实 LLM 调用记录 input/output/total token（兼容 `usage_metadata` 与 OpenAI 兼容
  接口常见的 `response_metadata.token_usage`）；不记录 API Key 与完整敏感 Prompt；
- 新增 `LLM_PRICE_PER_1K_INPUT` / `LLM_PRICE_PER_1K_OUTPUT`（单位 USD / 1K tokens）。
  **未配置单价时成本字段为 `null`，不写 0**；
- 评测报告输出总成本、平均每任务成本、单价来源与 `cost_price_assumed`，并明确
  "按配置单价估算、不是供应商真实账单、生产环境必须用真实账单校准"；
- OFFLINE 模式不调用外部模型，不产生 Token 与 LLM 成本。

### 决策 5：MCP 只读边界强化

沿用 ADR-003 的 MCP 授权边界，V1.1 进一步强化（不改协议范围）：

- 只暴露 6 个只读工具；`generate_draft` 与审批/建单/下单/收货/取消**绝不注册**；
- **身份合同**：actor 固定来自 `MCP_ACTOR_ID`，必须是业务库中**真实存在的用户 id**，默认
  `bob`（仅 operator 角色）；**不能填角色名**（如 `operator`，早期默认值即为此，会导致
  所有调用被拒）。指向不存在的用户时调用被拒绝、写审计，并返回**可操作**的修正提示；
  请求参数不能覆盖 actor（`inputSchema` 中不存在 `actor_id` / `user_id` 等参数），
  `system` 冒充被显式拒绝；
- **每次调用先完成角色校验，再执行查询**；参数非法、权限失败、查询异常与成功一律写审计；
- 审计记录 actor / action / tool / operation_id / 参数摘要 / 结果规模 / error_code，
  **不记录完整查询结果、完整 Prompt、密钥或敏感文本**；
- 输入边界（`days` 1~365、`top_k` 1~20、id 长度 ≤64、query 长度 ≤200、非空）通过
  `Annotated` + `Field` 进入 **MCP `inputSchema`（客户端可见）**，运行时二次校验保留。

### 决策 6：Agent 决策链语义追踪

每轮 trace 至少关联：`request_id`、`thread_id`、`plan_id`、**节点执行顺序**（图节点名 +
`step_count`）、**选择的工具**、**工具参数摘要**、**工具结果规模与稳定错误码**、
**RAG 引用的 `source_chunk_id` / `document_id`**、最终 `outcome`、`degradation_reason`、
`step_count`、`loop_blocked`。

约束：

- 不得记录密钥、完整 Prompt、完整检索正文或敏感载荷；
- 无检索命中时记录 `has_evidence=false` 与空引用列表，**不得虚构 citation**；
- Langfuse 无凭证时仍是安全 no-op，上述字段通过本地结构化 span 固定，并由单元测试断言
  （`tests/agent/test_semantic_trace.py` 注入 fake trace 捕获 span，不依赖云端凭证）。

## 后果

### 正面

- Agent 具备可编程主链路，便于集成、自动化测试与演示；
- 真实 LLM 与降级路径可解释、可统计，不存在静默降级；
- 失控防护把"转人工"变成确定性出口，避免写工具被循环触发；
- 成本字段诚实（`null` 而非 0），单价来源与假设性质可追溯；
- MCP 只读边界在代码、审计与测试三层落实。

### 代价与限制

- 本 ADR 的扩展能力**突破 V1 冻结**，必须与 V1 验收结论分别陈述；
- 真实 LLM 评测非确定性，需要真实 Key 才能产出；无 Key 时只报告 OFFLINE 基线与降级原因，
  不伪造真实模型指标；
- 成本为**假设单价估算**，不是供应商真实账单；
- 本机未运行 PostgreSQL/Docker 时，`db` 标记测试与容器 E2E 不执行，须如实标注；
- 节点级流式已实现，token 级流式仍只对真实 LLM 路径有意义。

## 不采用的方案

| 方案 | 不采用原因 |
|---|---|
| 降级只写日志、不留状态与 SSE 事件 | 静默降级无法审计与统计 |
| `LLM_MODE=llm` 缺配置时仍尝试调用并报"成功" | 伪造真实模型路径，违反诚实边界 |
| 未配置单价时把成本写成 0 | 0 会被误读为真实零成本 |
| 用异常文本作为降级原因 | 原因不稳定，无法评测聚合 |
| 只在节点内检测重复而不设节点级步数上限 | 无法覆盖未来新增环路 |
| MCP 暴露写工具由 client 自律 | 扩大本地授权边界，违反宪法第二十六条 |

## 与 V1 基线的关系

本 ADR 只新增 V1.1 扩展能力，不修改 ADR 001 的 V1 决策：单 Agent、确定性领域服务、
人工审批、数据库唯一事实源、幂等与 7 项安全不变量全部保持不变。
