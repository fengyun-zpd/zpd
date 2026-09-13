# 演进 ADR 索引

- [ADR-002 多 Agent 分阶段引入](ADR-002-multi-agent-strategy.md)
- [ADR-003 MCP 与 Mule Agent Bridge](ADR-003-mcp-mule-agent-fabric.md)
- [ADR-004 RAG 与长期记忆](ADR-004-rag-memory.md)
- [ADR-005 模型路由与微调](ADR-005-model-finetuning-eval.md)
- [ADR-006 可靠性与回退](ADR-006-reliability-fallback.md)
- [ADR-007 Agent 运行时门禁、LLM 模式与 HTTP 主链路](ADR-007-agent-runtime-gates.md)
- [ADR-008 SSE 事件传输持久化与 MCP 协议互操作验证](ADR-008-stream-transport-interoperability.md)

这些 ADR 只约束演进设计，不把 V1 规划能力写成已实现能力。

**V1.1 已实现部分**：ADR-003 的 MCP 只读 Server（6 个只读工具，stdio）、ADR-008 的
Redis SSE 传输缓冲/跨进程续传验证与
ADR-007 的 `LLM_MODE` 三态、HTTP Agent 主链路（agent/start、agent/resume、
agent/state）、Agent 步数与工具重复门禁、成本估算字段已在 V1.1 落地（代码 + 测试）；
其余能力（Mule Bridge、多 Agent、图记忆、微调等）仍为规划中。
