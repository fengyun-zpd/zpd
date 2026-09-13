# ADR-003：MCP 与 Mule Agent Bridge

## 状态

已接受（**V1.1 扩展**，部分实现）：MCP 只读工具 Server 在 **V1.1** 落地，**不属于 V1 冻结基线**（V1 冻结基线见 ADR 001 修订 1.8）；Mule Agent Bridge 仍为 V2/V3 规划，未实现。

验证边界（如实）：代码与单元/集成测试已落地（`tests/unit/test_mcp_server.py`、`tests/integration/test_mcp_tools.py`）；**未完成容器化部署验证，也未与真实 MCP 宿主（Claude Desktop 等）联调**。

## 决策

MCP 作为工具和 Agent 能力的跨进程契约；Function Calling 作为模型兼容层。Mule Agent Bridge 负责将本系统映射到 MuleSoft Agent Fabric/其他 Agent 网络，核心域服务不依赖 MuleSoft。

V1.1 落地范围：将 StockMind 的只读工具（`list_warehouses` / `list_products` / `get_inventory` / `get_demand_history` / `get_supplier_options` / `search_rules`）封装为 MCP Server（stdio transport，`app/mcp/server.py`），供外部 Agent 通过 MCP 协议只读查询。写工具 `generate_draft` 与审批、下单、收货、取消等能力绝不暴露。

## 约束

桥接层必须做身份映射、租户注入、Schema 校验、超时、审计和断路；外部 Agent 不能借桥接层扩大本地工具权限。

MCP Server 授权边界：

- 只读工具白名单与 `app.agent.tools.READ_ONLY_TOOLS` 一致，写工具绝不注册；
- 身份固定：MCP 客户端统一以环境变量 `MCP_ACTOR_ID` 执行，默认 `bob`——必须是业务库中**真实存在的用户 id**（仅 operator 角色），**不能填角色名**（如 `operator`）。指向不存在的用户时调用被拒绝、写审计并返回可操作的修正提示；外部不能通过请求参数指定 actor 冒充他人或 `system`（宪法第九条）；
- **角色校验先于实际查询**；成功、参数非法、越权与查询异常都写入审计（`app.services.audit.write_audit`），只记 actor / 操作 id / 参数摘要 / 结果规模 / 稳定错误码，不记录完整结果、完整 Prompt、密钥或敏感文本；
- 输入边界同时暴露给 MCP client（`Annotated` + `Field` 生成的 `inputSchema`：`days` 1~365、`top_k` 1~20、id 长度 ≤64、query 长度 ≤200、非空）并在运行时二次校验；
- MCP 只做协议适配与能力发现，不扩大本地授权边界（宪法第二十六条）。
