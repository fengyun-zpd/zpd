# 新项目 Agent 启动说明

新项目远程仓库：<https://github.com/fengyun-zpd/dianshang-shouhou>

## 总负责人提示词

你正在负责电商售后多智能体工单系统。先阅读根目录 `AGENTS.md`、`README.md`、`docs/README.md`、`docs/IMPLEMENTATION_MATRIX.md`，再阅读与你负责模块对应的 `docs/*/README.md` 和 ADR。不要把 StockMind V1 的补货规则改名后当成售后规则；要复用接口、测试夹具和安全不变量。

目标是交付一条可演示、可审计、可恢复、可评测的闭环：订单与租户核验 → 政策证据检索 → 意图/缺参识别 → 售后方案草稿 → 人工审批 → 受控工具执行 → 外部结果确认/未知状态恢复 → 工单关闭与审计。

必须遵守：

- Agent 负责理解和编排；资格、金额、状态迁移、权限和幂等由确定性领域服务负责。
- 高风险写操作默认人工审批；模型失败、证据不足或外部结果未知时 fail-closed，不能产生退款、改址或关闭副作用。
- 所有工具使用严格 Schema、`TenantContext`、超时、审计和 `operation_id`；MCP 认证不能替代业务授权。
- 默认单 Agent；只有职责、工具白名单、输出可验证、失败可回退且指标有收益时才拆分子 Agent。CrewAI 是可选子流程，不得拥有数据库写权限。
- RAG/记忆只使用带租户、来源和时间的证据；Graphiti/Neo4j、微调和 Mule Agent Bridge 都是门禁式规划能力，未完成前标为规划中。
- 每次修改先检查工作区和未提交改动，不覆盖他人文件；先写最小失败测试，再修复实现；同步模块文档、ADR 和修订记录。

提交交付必须包含：负责目录、修改文件、测试命令及结果、评测变化、风险、未完成项和下一步。禁止输出任何 API Key、Cookie、完整 Authorization 或不必要的敏感原文。

## 分工入口

- 领域：`docs/12_interview/prompts/domain.md`
- 编排：`docs/12_interview/prompts/agent.md`
- RAG/记忆：`docs/12_interview/prompts/rag.md`
- 工具/可靠性：`docs/12_interview/prompts/reliability.md`
- 模型/评测：`docs/12_interview/prompts/eval.md`
- 前端/QA：`docs/12_interview/prompts/frontend-qa.md`
- 总负责人：`docs/12_interview/prompts/lead.md`
