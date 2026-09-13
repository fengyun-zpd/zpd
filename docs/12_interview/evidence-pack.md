# 面试技术触点证据包

这份文档把面试中容易被追问的技术点绑定到代码、命令和诚实边界。所有数据仍是固定种子生成的合成演示数据。

## 一键验收

在 Windows PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_interview.ps1 -StopAfter
```

该入口会构建并启动隔离 Compose，执行关键后端回归、官方 MCP SDK stdio 验证、Redis SSE 跨进程续传验证和全部 Playwright E2E，最后销毁隔离容器与数据卷。关键测试出现 `skip` 会直接失败。

单独复验两个协议触点：

```powershell
$env:POSTGRES_DSN = "postgresql+psycopg://stockmind:stockmind@127.0.0.1:15432/stockmind"
$env:EMBEDDING_ENABLED = "false"
& .\backend\.venv\Scripts\python.exe .\scripts\verify_mcp_stdio.py --actor-id bob --python .\backend\.venv\Scripts\python.exe

$env:REDIS_URL = "redis://127.0.0.1:16379/0"
& .\backend\.venv\Scripts\python.exe .\scripts\verify_sse_redis.py
```

## 证据矩阵

| 追问点 | 代码/脚本 | 已验证内容 | 仍未声称 |
|---|---|---|---|
| MCP 协议 | `backend/app/mcp/server.py`、`scripts/verify_mcp_stdio.py` | 官方 SDK `initialize`、六个只读工具发现、`list_warehouses` 调用、写工具不可见 | 未与 Claude Desktop 等真实桌面宿主联调；MCP 不扩大本地权限 |
| SSE 断线续传 | `backend/app/streaming.py`、`scripts/verify_sse_redis.py` | Redis 事件带序号和 TTL，两个独立进程可恢复同一 turn，跨 thread 拒绝 | 不是生产压测，也不是完整进程崩溃和网络分区演练 |
| Agent 恢复 | `app/api/agent.py`、LangGraph checkpoint | `Last-Event-ID` 只重放未收到事件，缓冲缺失从 checkpoint 重放最小终态，不重新执行 Agent | checkpoint 不是业务事实源 |
| 多模态/OCR | `docs/05_models`、规划 ADR | 仅登记演进方向 | 当前 V1.1 未实现 PDF OCR/表格抽取 |
| 向量索引 | `docs/04_rag_memory` | pgvector 基线检索和黄金集 RAG 指标 | HNSW/IVF 量化报告尚未实测 |
| 模型路由/微调 | `docs/05_models` | OFFLINE、`auto`、`llm` 三态和成本字段 | 多模型路由、微调和生产账单校准尚未实现 |

## 真实 LLM 评测

黄金集现在包含 10 个参数样本、10 个对话样本、5 个 RAG 查询和 4 个预测样本：

```bash
cd backend
bash evaluation/run_eval.sh http://127.0.0.1:8000 offline
bash evaluation/run_eval.sh http://127.0.0.1:8000 llm
```

`offline` 会清空外部凭证并强制确定性路径。`llm` 只有在 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 都配置时才代表真实模型路径；缺少任一配置时报告会写 `llm_configured=false`、`LLM_NOT_CONFIGURED` 和“**不代表真实模型结果**”。成本单价来自 `LLM_PRICE_PER_1K_INPUT/OUTPUT` 时也必须标注为假设单价，不是供应商账单。

P50/P95 使用 nearest-rank 计算。延迟、Token 和成本均是本次单线程小样本实测，不外推为生产容量或真实收益。

## 面试回答边界

- Agent 负责理解、证据检索和编排；确定性领域服务负责预测、供应商和补货量；授权人员负责审批及外部副作用。
- MCP 是协议适配层，Redis 是短期 SSE 传输层；PostgreSQL 仍是业务事实源，LangGraph checkpoint 只负责会话恢复。
- 没有真实 Key 的机器不报告真实 LLM 准确率、延迟或成本；没有真实索引实验不报告 HNSW/IVF 优劣。
