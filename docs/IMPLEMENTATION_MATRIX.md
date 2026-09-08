# 实现矩阵与模块责任

这张表是新项目从 StockMind V1 迁移能力时的唯一导航之一。状态必须以代码、测试和评测证据为准。

| 模块目录 | 新项目建议代码边界 | 当前证据 | 首批测试 | 状态 |
|---|---|---|---|---|
| `00_product` | `docs/`、README、演示脚本 | StockMind V1 闭环与验收报告 | 文档链接/演示冒烟 | 已有基线 |
| `01_domain` | `src/domain/after_sales` | StockMind 确定性计算、状态机、幂等 | 纯函数、状态机、性质测试 | 售后插件规划 |
| `02_agents` | `src/agents/supervisor`、`src/agents/subagents` | StockMind LangGraph 单 Agent | 路由、Schema、checkpoint、回放 | 单 Agent 基线/多 Agent 规划 |
| `03_tools` | `src/platform/tools`、`src/integrations/mcp` | StockMind 工具白名单与受控草稿 | Schema、权限、超时、审计 | MCP 规划 |
| `04_rag_memory` | `src/rag`、`src/memory` | StockMind FTS/pgvector/RRF | Recall/MRR、引用、注入、租户 | RAG 基线/图记忆规划 |
| `05_models` | `src/models`、`experiments/finetune` | StockMind OFFLINE/真实 LLM 双模式 | 能力矩阵、成本、P95、LoRA 对比 | 路由基线/微调规划 |
| `06_reliability` | `src/platform/reliability` | StockMind order_unknown、故障注入 | 重试、熔断、回退、未知状态 | 原则已有/售后适配规划 |
| `07_sessions` | `src/platform/sessions` | StockMind LangGraph checkpoint | 并发恢复、超时、租户越权 | 会话基线/多租户增强规划 |
| `08_evaluation` | `evals/`、`observability/` | StockMind 黄金集、Langfuse 可选 | 双模式回归、轨迹回放、安全不变量 | 已有基线 |
| `09_frontend` | `frontend/`、`src/api` | StockMind 八页面与 SSE | Playwright 主流程、断线恢复 | 售后工作台规划 |
| `10_operations` | `deploy/`、`infra/`、CI | StockMind Compose、隔离部署、CI | 迁移、备份、容量、故障演练 | 本地基线/生产规划 |
| `11_testing` | `tests/unit|integration|e2e|property` | StockMind 分层测试 | 每次变更的最小回归集 | 已有基线 |

## 迁移原则

先复制接口与测试夹具，再迁移实现；不要把 StockMind 的补货规则直接改名成售后规则。每个模块在新仓库中独立提交，提交说明包含 `scope`、测试命令、评测结果和未完成项。
