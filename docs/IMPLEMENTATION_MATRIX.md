# 实现矩阵与模块责任

这张表是新项目从 StockMind V1 迁移能力时的唯一导航之一。状态必须以代码、测试和评测证据为准。

| 模块目录 | 新项目建议代码边界 | 当前证据 | 首批测试 | 状态 |
|---|---|---|---|---|
| `00_product` | `docs/`、README、演示脚本 | StockMind V1 闭环与验收报告 | 文档链接/演示冒烟 | 已有基线 |
| `01_domain` | `src/domain/after_sales` | StockMind 确定性计算、状态机、幂等 | 纯函数、状态机、性质测试 | 售后插件规划 |
| `02_agents` | `src/agents/supervisor`、`src/agents/subagents` | StockMind LangGraph 单 Agent；HTTP Agent 主链路（`agent/start|resume|state`）与步数/工具重复门禁、`escalate` 转人工（V1.1） | 路由、Schema、checkpoint、回放、门禁触发与转人工 | 单 Agent 基线 + V1.1 主链路 / 多 Agent 规划 |
| `03_tools` | `src/platform/tools`、`src/integrations/mcp` | StockMind 工具白名单与受控草稿；MCP 只读 Server（`app/mcp/server.py`），官方 SDK stdio 验证脚本 | Schema、权限、超时、审计；MCP 白名单不变量；协议握手与只读调用 | MCP 只读已实现（V1.1）/ Mule Bridge 规划 |
| `04_rag_memory` | `src/rag`、`src/memory` | StockMind FTS/pgvector/RRF | Recall/MRR、引用、注入、租户 | RAG 基线/图记忆规划 |
| `05_models` | `src/models`、`experiments/finetune` | StockMind OFFLINE/真实 LLM 双模式 | 能力矩阵、成本、P95、LoRA 对比 | 路由基线/微调规划 |
| `06_reliability` | `src/platform/reliability` | StockMind order_unknown、故障注入 | 重试、熔断、回退、未知状态 | 原则已有/售后适配规划 |
| `07_sessions` | `src/platform/sessions` | StockMind LangGraph checkpoint；审批恢复的计划与会话绑定校验（thread/plan/version/checkpoint，V1.1） | 并发恢复、超时、租户越权、跨会话恢复拒绝 | 会话基线 + V1.1 绑定校验 / 多租户增强规划 |
| `08_evaluation` | `evals/`、`observability/` | StockMind 黄金集、Langfuse 可选；决策链语义追踪（节点顺序 / 工具摘要 / 结果规模 / RAG 引用，V1.1） | 双模式回归、轨迹回放、语义追踪断言、安全不变量 | 已有基线 + V1.1 语义追踪 |
| `09_frontend` | `frontend/`、`src/api` | StockMind 八页面与 Redis/内存 SSE 续传 | Playwright 主流程、断线恢复、跨进程事件读取 | 售后工作台规划 |
| `10_operations` | `deploy/`、`infra/`、CI | StockMind Compose、隔离部署、CI | 迁移、备份、容量、故障演练 | 本地基线/生产规划 |
| `11_testing` | `tests/unit|integration|e2e|property` | StockMind 分层测试；夹具显式注册 ORM 模型并同步 app 连接池（V1.1 修复，消除 `relation "warehouse" does not exist` 竞态） | 每次变更的最小回归集、夹具隔离性回归 | 已有基线 + 夹具健康度修复 |

## 迁移原则

先复制接口与测试夹具，再迁移实现；不要把 StockMind 的补货规则直接改名成售后规则。每个模块在新仓库中独立提交，提交说明包含 `scope`、测试命令、评测结果和未完成项。
