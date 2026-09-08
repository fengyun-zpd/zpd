# 工具调用、MCP 与外部集成契约

## 工具分层

- 只读：`get_order`、`get_logistics`、`get_customer_profile`、`retrieve_policy`、`get_ticket`。
- 受控写：`create_ticket_draft`、`create_refund_draft`、`submit_approval`。
- 人工/后台命令：`approve_operation`、`execute_refund`、`change_address`、`close_ticket`、`reconcile_operation`。

所有工具使用严格 JSON Schema、输入规范化、输出模型校验、超时、审计和 `operation_id`。MCP 身份认证不等于业务授权，授权必须在服务端根据 `TenantContext` 和角色再次判断。

## 工具注册策略

工具少于约 10 个时使用域隔离注册；工具规模扩大后才引入动态工具检索。动态检索只能减少上下文，不能扩大权限白名单。

## 外部 Agent 网络

增加 `MuleAgentBridge` 适配器，将本系统能力以 MCP/A2A 兼容服务暴露给 MuleSoft Agent Fabric 或其他 Agent 网络。桥接层只负责协议转换、身份映射、超时和审计；核心业务仍由本地领域服务裁决。
