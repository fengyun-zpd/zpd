"""MCP 工具端到端测试（db）：经协议调用只读工具 + 成功/失败审计 + 未授权 actor。

边界：MCP 只做协议适配，不扩大本地授权边界（宪法第二十六条）；角色校验先于查询，
成功、参数非法、权限失败与查询异常都必须留审计。
"""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select

from app.agent import tools as agent_tools
from app.mcp import server as mcp_server
from app.models.governance import AuditLog


@pytest.mark.db
async def test_mcp_lists_only_readonly_tools_via_protocol(db_session):
    """经 MCP 协议列工具：写工具不可见（读 ``.tools``，不迭代结果对象本身）。"""
    async with create_connected_server_and_client_session(mcp_server.mcp) as client:
        result = await client.list_tools()
        names = {tool.name for tool in result.tools}
        assert names == set(mcp_server.READ_ONLY_TOOLS)
        for forbidden in mcp_server.FORBIDDEN_TOOLS:
            assert forbidden not in names


@pytest.mark.db
async def test_mcp_write_tool_not_callable_via_protocol(db_session):
    """外部 MCP client 无法调用写工具 generate_draft（未注册即不可见）。"""
    async with create_connected_server_and_client_session(mcp_server.mcp) as client:
        result = await client.list_tools()
        assert "generate_draft" not in {tool.name for tool in result.tools}


@pytest.fixture()
def mcp_actor(monkeypatch) -> str:
    """把 MCP actor 固定为种子中真实存在的用户（bob，仅 operator 角色）。

    默认值 ``operator`` 是**角色名**而非用户 id，直接使用会被角色校验拒绝（FORBIDDEN）；
    因此测试显式指向存在的用户，并采用最小权限的那个。
    """
    monkeypatch.setattr(mcp_server, "_ACTOR_ID", "bob")
    return "bob"


@pytest.mark.db
async def test_mcp_success_writes_audit_without_result_body(db_session, mcp_actor):
    """成功调用写审计：actor 固定、参数摘要与结果规模；不写入结果全文。"""
    async with create_connected_server_and_client_session(mcp_server.mcp) as client:
        result = await client.call_tool("list_warehouses", {})
        assert result.content and result.content[0].text
        assert "warehouse_id" in result.content[0].text

    logs = db_session.scalars(select(AuditLog).where(AuditLog.action == "mcp.list_warehouses")).all()
    assert logs, "MCP 成功调用应写入审计"
    log = logs[0]
    assert log.actor_id == "bob"
    assert log.error_code is None
    state = log.after_state or {}
    assert state.get("ok") is True
    assert state.get("operation_id")
    assert state.get("result_size") is not None
    # 审计不得包含查询结果正文
    assert "warehouse_id" not in str(state)


@pytest.mark.db
async def test_mcp_invalid_argument_no_query_but_audited(db_session, mcp_actor, monkeypatch):
    """参数非法：不触发真实查询，但仍产生审计（稳定 error_code）。"""
    called = {"count": 0}
    original = agent_tools.get_inventory

    def _spy(*args, **kwargs):
        called["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_tools, "get_inventory", _spy)

    with pytest.raises(mcp_server.MCPToolError) as exc:
        mcp_server.get_inventory("", "SKU-E01")
    assert exc.value.code == "INVALID_ARGUMENT"
    assert called["count"] == 0, "非法参数不得触发真实查询"

    logs = db_session.scalars(select(AuditLog).where(AuditLog.action == "mcp.get_inventory")).all()
    assert logs, "参数非法也必须留审计"
    assert logs[0].error_code == "INVALID_ARGUMENT"


@pytest.mark.db
async def test_mcp_unauthorized_actor_no_query_but_audited(db_session, monkeypatch):
    """未授权 actor（system 冒充）：先拒绝，不触发查询，并写审计。"""
    monkeypatch.setattr(mcp_server, "_ACTOR_ID", "system")
    called = {"count": 0}
    original = agent_tools.list_warehouses

    def _spy():
        called["count"] += 1
        return original()

    monkeypatch.setattr(agent_tools, "list_warehouses", _spy)

    with pytest.raises(mcp_server.MCPToolError) as exc:
        mcp_server.list_warehouses()
    assert exc.value.code == "FORBIDDEN_ACTOR"
    assert called["count"] == 0, "未授权 actor 不得触发真实查询"

    logs = db_session.scalars(select(AuditLog).where(AuditLog.action == "mcp.list_warehouses")).all()
    assert logs, "权限失败也必须留审计"
    assert logs[0].error_code == "FORBIDDEN_ACTOR"


@pytest.mark.db
async def test_mcp_unknown_actor_returns_actionable_error(db_session, monkeypatch):
    """actor 指向不存在的用户（如角色名 operator）：拒绝并给出可操作的修正提示。"""
    monkeypatch.setattr(mcp_server, "_ACTOR_ID", "operator")  # 角色名而非用户 id
    called = {"count": 0}
    original = agent_tools.list_warehouses

    def _spy():
        called["count"] += 1
        return original()

    monkeypatch.setattr(agent_tools, "list_warehouses", _spy)

    with pytest.raises(mcp_server.MCPToolError) as exc:
        mcp_server.list_warehouses()
    assert exc.value.code == "FORBIDDEN_ACTOR"
    assert called["count"] == 0, "未知 actor 不得触发真实查询"
    # 错误信息必须可操作：指出应配置真实用户 id
    assert "MCP_ACTOR_ID" in exc.value.message
    assert "bob" in exc.value.message

    logs = db_session.scalars(select(AuditLog).where(AuditLog.action == "mcp.list_warehouses")).all()
    assert logs
    assert logs[0].error_code == "FORBIDDEN_ACTOR"


@pytest.mark.db
async def test_mcp_audit_contains_operation_id_and_result_size(db_session, mcp_actor):
    """审计必须包含 operation_id、actor、工具名与结果规模（不写结果全文）。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(mcp_server.mcp) as client:
        await client.call_tool("list_products", {})

    logs = db_session.scalars(select(AuditLog).where(AuditLog.action == "mcp.list_products")).all()
    assert logs
    log = logs[0]
    assert log.actor_id == "bob"
    assert log.entity_id == "list_products"
    assert log.request_id, "以 operation_id 关联一次调用"
    state = log.after_state or {}
    assert state.get("operation_id")
    assert state.get("result_size") is not None
    assert state.get("ok") is True
