# LLM 端点、路由、评测与微调

## 端点契约

统一 OpenAI-compatible client，支持本地 vLLM/Ollama 或云端模型。端点必须经过 host/IP 白名单、密钥存在性、超时和健康检查；失败时 fail-closed，不产生写副作用。

## 能力矩阵

按 `intent`、`structured_extract`、`tool_call`、`long_context`、`zh_quality`、`cost`、`latency` 和高风险场景单独评测。只有满足写入门槛的模型才允许生成受控草稿，审批和执行不由模型决定。

## 微调路线

1. 先建立 Prompt + RAG + 工具基线。
2. 从脱敏工单构造 `instruction/input/output/evidence_refs/risk_label` 数据集，人工复核并拆分 train/validation/test。
3. 先做 LoRA/QLoRA 意图分类、字段抽取、工具选择实验；不要微调业务金额和状态迁移。
4. 用同一黄金集比较基线与微调模型，记录准确率、拒答率、幻觉率、工具正确率、P95、Token 和成本。
5. 微调模型必须经过回滚、漂移监控和安全不变量回归。
