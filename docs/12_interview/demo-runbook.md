# StockMind 五分钟演示路径

这条路径展示一个可审计的补货决策闭环。演示使用固定随机种子的隔离 Compose 环境，所有数据均为合成数据。

## 准备

在仓库根目录执行：

```powershell
.\scripts\verify_interview.ps1
```

验收通过后保留窗口和浏览器页面，打开 `http://127.0.0.1:13000`。无需 LLM Key，页面会明确显示 OFFLINE 模式；配置 Key 时可切换到真实 LLM，但不要把小样本延迟当成容量结论。

## 现场顺序

### 1. 缺参澄清（约 30 秒）

进入“补货助手”，先发送“帮我补货”。

预期：助手用业务语言要求补充仓库、SKU/商品范围和规划周期，不暴露 `warehouse_id` 等内部字段名，也不把预算当作 V1 必填参数。

### 2. 生成补货草稿（约 45 秒）

补充一个仓库、商品范围和 7/14/30 天规划周期，发送请求。

预期：助手先检索规则和库存证据，再调用唯一写工具“生成补货草稿”。页面显示计划、计算结果、证据引用和审计关联信息；数量由确定性领域服务计算。

### 3. 人工审批（约 45 秒）

打开“审批箱”，查看计划明细、供应商、补货数量和阻断原因，批准有效明细。

预期：会话在草稿提交后暂停，审批由页面角色完成；Agent 不直接批准、不创建采购单。

### 4. 触发 `PLAN_STALE`（约 45 秒）

在审批后修改一项库存或规则输入，再尝试建单。

预期：接口返回 HTTP 409 `PLAN_STALE`，页面列出变化字段；系统不部分建单、不静默重算。选择排除变化明细或由操作员生成整单修订版后才能继续。

### 5. 建单与下单未知状态恢复（约 60 秒）

对批准计划按供应商建单，进入采购单页面；在模拟供应商管理中将一个供应商切换为超时，再执行下单。

预期：系统先持久化下单尝试，再进入 `order_unknown`；恢复动作只能沿用原幂等键查询供应商事实，不能盲目重试或换键重下。审计记录包含操作者、状态前后值和操作幂等信息。

### 6. 分批收货与审计回放（约 45 秒）

对采购单分两次收货，最后打开执行记录或计划详情。

预期：收货数量和库存更新在事务中完成，未收齐时保持 `partially_received`，收齐后才进入 `received`/关闭；执行记录可回看规则证据、输入哈希、计算版本、状态迁移和 trace 关联。

## 讲解收束

最后用一句话概括边界：

> LLM 负责理解、检索和编排；确定性服务负责计算和状态；授权人员负责副作用。当前是本机合成数据 V1 演示，不是已接入真实 WMS/ERP 的生产系统。

## V1.1 扩展演示（可选，约 90 秒）

> 以下能力属 **V1.1 扩展**，不在 V1 冻结基线内。演示前请确认后端已启动且数据库可用。

**1）HTTP Agent 主链路**（不再只靠页面驱动）：

```powershell
# 启动一轮（SSE 逐节点事件；响应头含 X-Thread-Id）
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/agent/start `
  -H "X-Actor-Id: alice" -H "Content-Type: application/json" `
  -d '{\"content\": \"帮我补货\"}'

# 读取安全状态摘要（不含密钥 / 完整 Prompt / 检索正文）
curl.exe http://127.0.0.1:8000/api/v1/agent/<thread_id>/state -H "X-Actor-Id: alice"
```

预期：`agent_start → node_end(classify) → … → message → done`，事件带 `degradation_reason` /
`step_count` / `loop_blocked`。审批恢复只读取已提交决定；用**其他会话**的 `plan_id` 恢复会被拒（403），
版本不匹配返回 409。

**2）MCP 只读 Server**（stdio，仅 6 个只读工具）：

```powershell
$env:MCP_ACTOR_ID = "bob"     # 必须是业务库中真实存在的用户 id
.\.venv\Scripts\python.exe -m app.mcp.server
```

用 MCP Inspector 或任意 MCP client 连接后 `list_tools` 应只看到 6 个只读工具；
`generate_draft` 与审批/下单等写能力**不可见**。`inputSchema` 中可见 `days` 1~365、
`top_k` 1~20、id ≤64、query ≤200 等边界。

**3）决策链语义追踪（可选）**：配置 Langfuse 凭证后，每轮 trace 可回放节点执行顺序、
工具名与参数摘要、结果规模、RAG 引用的文档块 ID、降级原因、步数与门禁状态；未配置凭证时
为安全 no-op（`tests/agent/test_semantic_trace.py` 用 fake trace 固定字段范围，不依赖云端）。
另：断线续传的 `Last-Event-ID` 非法时返回 422，**不会静默开启新一轮任务**。

**4）诚实边界（演示时必须说明）**：MCP Server 仅在自动化测试中验证过（SDK in-memory client），
**未与真实 MCP 宿主（Claude Desktop 等）联调，也未做容器化部署验证**；成本字段是按配置
**假设单价**估算，不是供应商真实账单。

演示结束后可执行：

```powershell
docker compose -f docker-compose.iso.yml -p stockmind-interview down -v
```
