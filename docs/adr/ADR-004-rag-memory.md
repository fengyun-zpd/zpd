# ADR-004：RAG 与长期记忆

## 状态

提议，V2 规划，未实现 Graphiti/Neo4j 部分。

## 决策

先采用 PostgreSQL/pgvector + 关键词基线，检索结果必须带来源和租户过滤。Graphiti/Neo4j 只存带时间的显式事实并异步写入；记忆故障不阻断当前工单。
