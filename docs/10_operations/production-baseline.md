# 生产基线与交付门禁

## 本地

Docker Compose 提供 API、Worker、PostgreSQL/pgvector、Redis、前端和模拟外部系统；可选启用 Milvus、Neo4j、Langfuse。默认关闭高成本依赖也能跑通主闭环。

## 预生产/生产门禁

必须完成多租户隔离测试、密钥扫描、迁移回滚、容量压测、故障注入、备份恢复、人工审批演练、模型回滚和未知状态对账。没有这些证据只能称为本地演示。

## 可观测

统一 `request_id/trace_id/thread_id/ticket_id/operation_id`；日志脱敏，指标按租户和模型聚合，Prompt 与工具参数按最小必要原则留存。
