# ADR-003：MCP 与 Mule Agent Bridge

## 状态

提议，V2/V3 规划，未实现。

## 决策

MCP 作为工具和 Agent 能力的跨进程契约；Function Calling 作为模型兼容层。Mule Agent Bridge 负责将本系统映射到 MuleSoft Agent Fabric/其他 Agent 网络，核心域服务不依赖 MuleSoft。

## 约束

桥接层必须做身份映射、租户注入、Schema 校验、超时、审计和断路；外部 Agent 不能借桥接层扩大本地工具权限。
