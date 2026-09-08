# 多 Agent 编排与子 Agent 边界

## 分层

1. `SupervisorGraph`：理解请求、选择业务路由、管理 checkpoint、审批中断和恢复。
2. 领域子 Agent：订单核验、物流调查、政策检索、退款方案。每个子 Agent 只能访问自己的工具白名单。
3. 确定性服务：资格、金额、状态、幂等和权限。

LangGraph 是主状态机。CrewAI 只在“需要角色协作/并行研究”的子流程中作为可选适配层，不能拥有数据库写权限，也不能绕过 supervisor 的状态和审批。

## 统一状态

`tenant_id`、`thread_id`、`ticket_id`、`operation_id`、`approval_id`、`trace_id`、`agent_run_id`、`evidence_refs`、`risk_level`、`next_action`、`failure_code`。

## 何时拆 Agent

必须同时满足：职责边界可描述、工具集合可隔离、输出可验证、失败可回退、拆分后指标改善。否则保留单 Agent 子图并记录 ADR。

## 禁止事项

子 Agent 不得自行发现未注册工具，不得把自然语言金额写入退款命令，不得伪造审批结果，不得把另一个租户的数据作为上下文。
