# StockMind 模块化演进文档

> 新项目远程仓库：<https://github.com/fengyun-zpd/dianshang-shouhou>
>
> 本目录是 V1 仓储补货基线向“电商售后多智能体工单系统”演进的模块化设计区。文档中的 `V1 已实现`、`V2 规划`、`V3 规划` 必须与代码和验收证据分开理解。

## 1. 为什么选这个方向

面试价值最高的不是把多个 Agent 拼在一起，而是展示一条可以上线、可以解释、可以恢复的业务闭环：用户提出售后问题，系统完成身份与订单上下文核验、政策证据检索、意图分类、方案生成、工具执行、人工审批、外部结果确认和审计追踪。

StockMind 的补货域保留为第一个确定性业务插件；电商售后工单域作为新项目的主业务插件。两者共享 Agent 平台能力，但不共享未经验证的业务规则。

## 2. 事实源与状态语义

| 内容 | 权威位置 | 状态 |
|---|---|---|
| V1 可验收行为 | `StockMind需求规格说明书.md` | 已实现/已验收 |
| V1 架构边界 | `StockMind架构设计文档.md`、`StockMind架构决策记录ADR001.md` | 已接受 |
| 自动代理行为约束 | `AGENTS.md` | 宪法约束 |
| 模块拆分与 V2/V3 路线 | 本目录 | 规划与拆解 |
| 新项目远程 | `fengyun-zpd/dianshang-shouhou` | 当前公开仓库为空，待初始化 |

**已实现**只写有代码、测试或可复现实测证据的内容；**规划中**只表示设计目标；**实验中**必须有实验编号、数据集、模型、指标和结论。

## 3. 模块地图

- [实现矩阵与模块责任](IMPLEMENTATION_MATRIX.md)

- [00 产品与面试命题](00_product/README.md)
- [01 领域边界与业务插件](01_domain/README.md)
- [02 多 Agent 编排](02_agents/README.md)
- [03 工具、MCP 与集成](03_tools/README.md)
- [04 RAG 与记忆](04_rag_memory/README.md)
- [05 模型路由与微调](05_models/README.md)
- [06 可靠性、回退与安全](06_reliability/README.md)
- [07 租户、会话与线程](07_sessions/README.md)
- [08 评测、回放与可观测](08_evaluation/README.md)
- [09 前端工作台](09_frontend/README.md)
- [10 生产基线与交付](10_operations/README.md)
- [11 测试、Bug 与迭代](11_testing/README.md)
- [12 面试叙事与 Agent 提示词](12_interview/README.md)
- [架构决策记录](adr/README.md)

## 4. 推荐实施顺序

1. 先保持 StockMind V1 回归通过，抽出平台接口和测试夹具。
2. 新增售后工单领域插件，先用单 Agent + 确定性服务完成闭环。
3. 用 LangGraph supervisor 引入受控子 Agent；只有跨域协作有收益时才启用 CrewAI。
4. 加入 MCP 工具契约、租户拦截器、回退状态机和黄金集评测。
5. RAG 与记忆先走 PostgreSQL/pgvector 基线，Graphiti/Neo4j 作为可插拔增强。
6. 完成模型矩阵评测后再做 LoRA/QLoRA 微调；微调模型不能绕过写入门禁。
7. 最后接入 MuleSoft Agent Fabric/Mule Agent Bridge 适配器，作为外部 Agent 网络演示，不作为核心运行时依赖。

## 5. 当前不做的事

- 不把“多 Agent”当作目标本身；没有可量化收益就保持单 Agent。
- 不让 LLM 计算金额、库存、退款额度或直接执行退款/改址等高风险副作用。
- 不把演示用合成数据、离线模型结果或本机延迟写成生产结论。
- 不因为引入 CrewAI、Neo4j、Milvus 或 MuleSoft 就声称系统已经生产可用。
