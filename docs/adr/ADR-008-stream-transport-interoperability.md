# ADR-008：SSE 事件传输持久化与 MCP 协议互操作验证

## 状态

已接受（**V1.1 扩展**）。Redis 事件传输后端和官方 MCP SDK stdio 验证属于 V1.1
能力，不改变 V1 冻结基线、业务状态机或安全不变量。

## 背景

节点级 SSE 已能在单进程内缓存事件并使用 `Last-Event-ID` 续传，但进程重启或多 API
进程切换会丢失传输缓冲。LangGraph checkpoint 可以恢复最终状态，却不能提供已经发送到
客户端的完整进度序列。MCP Server 已实现 stdio 工具，但只做 in-memory client 测试不足以
证明真实协议握手和工具发现链路。

## 决策

1. `app/streaming.py` 增加可选 `RedisStreamBuffer`。Compose 默认使用 `STREAM_EVENT_BACKEND=redis`；本机单测和 Redis 不可用时回退容量受限的 `memory` 缓冲。
2. Redis 只保存短期 SSE 传输事件：按 `turn_id` 存事件序号、线程归属、完成标记、TTL 和容量上限；不保存业务决定，不替代 PostgreSQL 或 LangGraph checkpoint。
3. 缓冲完整时只重放 `seq > Last-Event-ID` 的事件；缓冲缺失或未完成时读取 checkpoint 生成最小 `message + done` 兜底；不重新执行 Agent，不重复调用 `generate_draft`。
4. MCP 继续使用 stdio transport 和固定 `MCP_ACTOR_ID`。新增 `scripts/verify_mcp_stdio.py`，通过官方 Python SDK 完成 `initialize`、`list_tools` 和一个只读调用，断言工具集合严格等于六个只读工具。
5. 新增 `scripts/verify_sse_redis.py`，用两个独立 Python 子进程写入和读取同一 Redis turn，验证进程重建后的顺序、完成状态和跨 thread 拒绝。

## 验证与边界

- 单元测试覆盖内存缓冲、Redis 替身、跨 thread 拒绝和已完成 turn 不重复补流。
- `scripts/verify_interview.ps1 -StopAfter` 在隔离 Compose 中执行真实 Redis SSE 跨进程验证、MCP stdio 验证和浏览器 E2E。
- 传输层跨进程验证不等同于完整进程崩溃演练；真实 MCP 宿主（如 Claude Desktop）仍需单独联调。
- Redis 不可用时回退内存会缩短续传窗口；业务正确性仍由数据库事务、幂等键和 checkpoint 保障。

## 取舍

采用 Redis 是为了在本机 Compose 和多 API 进程之间共享短期事件，避免把业务事件写入新数据库表。代价是增加 Redis 可用性依赖和清理策略，因此所有事件都有 TTL/容量上限，且设置了安全回退路径。
