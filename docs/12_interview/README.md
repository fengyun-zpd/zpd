# 12 面试叙事与 Agent 提示词

- [技术触点证据包](evidence-pack.md)

这个项目的面试主线是：**我没有让 LLM 直接碰业务副作用，而是做了一个有证据、有审批、有幂等、有回退、有评测的 Agent 平台。**

## 推荐讲法

1. 先演示售后工单闭环。
2. 再展示一个拒绝越权/缺证据的案例。
3. 展示模型矩阵和黄金集，而不是只报一个准确率。
4. 展示 `operation_unknown` 恢复和审计轨迹。
5. 最后说明多 Agent、微调和 Mule Agent Bridge 的门禁式演进。

## 可直接分配给其他 Agent 的提示词

- [总负责人](prompts/lead.md)
- [领域插件](prompts/domain.md)
- [多 Agent 编排](prompts/agent.md)
- [RAG 与记忆](prompts/rag.md)
- [工具与可靠性](prompts/reliability.md)
- [模型与评测](prompts/eval.md)
- [前端与 QA](prompts/frontend-qa.md)
