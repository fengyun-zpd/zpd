"""MCP Server 只读白名单与输入边界测试（无 DB，ADR-003 / 宪法第九、二十六条）。"""

from __future__ import annotations

import pytest

from app.agent.tools import TOOL_WHITELIST
from app.mcp.server import (
    FORBIDDEN_TOOLS,
    MAX_DAYS,
    MAX_ID_LEN,
    MAX_QUERY_LEN,
    MAX_TOP_K,
    MIN_DAYS,
    MIN_TOP_K,
    READ_ONLY_TOOLS,
    MCPToolError,
    _validate_days,
    _validate_id,
    _validate_query,
    _validate_top_k,
    mcp,
)


def _registered_names() -> set[str]:
    """已注册的 MCP 工具名（FastMCP 内部工具登记表，元素为 Tool 对象）。"""
    return {t.name for t in mcp._tool_manager.list_tools()}


def test_mcp_exposes_only_readonly_tools():
    """MCP 只暴露只读工具；写工具 generate_draft 绝不注册。"""
    names = _registered_names()
    assert names == set(READ_ONLY_TOOLS)
    # 写工具在内部 Agent 白名单内，但绝不通过 MCP 暴露
    assert "generate_draft" in TOOL_WHITELIST
    assert names < TOOL_WHITELIST


def test_mcp_never_exposes_side_effect_tools():
    """审批、建单、下单、收货、取消等副作用能力一律不可见。"""
    names = _registered_names()
    for forbidden in FORBIDDEN_TOOLS:
        assert forbidden not in names, f"MCP 不得暴露副作用能力：{forbidden}"


def test_mcp_instructions_deny_side_effects():
    """MCP 指令文本明确声明无写操作/审批/下单能力。"""
    text = mcp.instructions or ""
    for forbidden in ("写操作", "审批", "下单"):
        assert forbidden in text


@pytest.mark.parametrize("value", [0, -1, MAX_DAYS + 1, "90", None, True])
def test_validate_days_rejects_invalid(value):
    with pytest.raises(MCPToolError) as exc:
        _validate_days(value)
    assert exc.value.code == "INVALID_ARGUMENT"


def test_validate_days_accepts_bounds():
    assert _validate_days(1) == 1
    assert _validate_days(MAX_DAYS) == MAX_DAYS


@pytest.mark.parametrize("value", [0, -1, MAX_TOP_K + 1, "5", None, False])
def test_validate_top_k_rejects_invalid(value):
    with pytest.raises(MCPToolError) as exc:
        _validate_top_k(value)
    assert exc.value.code == "INVALID_ARGUMENT"


def test_validate_top_k_accepts_bounds():
    assert _validate_top_k(1) == 1
    assert _validate_top_k(MAX_TOP_K) == MAX_TOP_K


@pytest.mark.parametrize("value", ["", "   ", None, "x" * (MAX_ID_LEN + 1), 123])
def test_validate_id_rejects_invalid(value):
    with pytest.raises(MCPToolError) as exc:
        _validate_id("warehouse_id", value)
    assert exc.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("value", ["", "   ", None, "x" * (MAX_QUERY_LEN + 1)])
def test_validate_query_rejects_invalid(value):
    with pytest.raises(MCPToolError) as exc:
        _validate_query(value)
    assert exc.value.code == "INVALID_ARGUMENT"


# ---------------------------------------------------------------- 身份合同与协议契约


def _tool(name: str):
    return next(t for t in mcp._tool_manager.list_tools() if t.name == name)


def test_default_actor_is_real_seed_user():
    """默认 actor 必须是种子中真实存在的用户 id，而不是角色名（如 operator）。"""
    from app.mcp.server import DEFAULT_MCP_ACTOR_ID

    assert DEFAULT_MCP_ACTOR_ID == "bob", "默认 actor 应指向最小权限的真实用户"
    assert DEFAULT_MCP_ACTOR_ID != "operator", "operator 是角色名，不能作为用户 id"


def test_tool_schemas_never_expose_actor_parameter():
    """请求参数不得包含 actor —— actor 只能来自 MCP_ACTOR_ID，客户端不可覆盖。"""
    for tool in mcp._tool_manager.list_tools():
        props = (tool.parameters or {}).get("properties", {}) or {}
        for forbidden in ("actor_id", "actor", "user_id", "principal_id"):
            assert forbidden not in props, f"{tool.name} 不得暴露可覆盖 actor 的参数：{forbidden}"


def test_input_schema_declares_bounds():
    """客户端可见的 schema 必须包含关键输入边界（最小值/最大值/长度）。"""
    demand = _tool("get_demand_history").parameters["properties"]
    assert demand["days"]["minimum"] == MIN_DAYS
    assert demand["days"]["maximum"] == MAX_DAYS
    assert demand["warehouse_id"]["minLength"] == 1
    assert demand["warehouse_id"]["maxLength"] == MAX_ID_LEN

    rules = _tool("search_rules").parameters["properties"]
    assert rules["top_k"]["minimum"] == MIN_TOP_K
    assert rules["top_k"]["maximum"] == MAX_TOP_K
    assert rules["query"]["minLength"] == 1
    assert rules["query"]["maxLength"] == MAX_QUERY_LEN

    # 可选字符串参数的约束位于 anyOf 内，客户端同样可见
    category = _tool("list_products").parameters["properties"]["category"]
    branch = category.get("anyOf") or [category]
    assert any(item.get("maxLength") == MAX_ID_LEN for item in branch)


def test_required_arguments_declared_in_schema():
    """必填参数必须在 schema 的 required 中声明（客户端可见）。"""
    assert set(_tool("get_inventory").parameters["required"]) == {"warehouse_id", "product_id"}
    assert set(_tool("get_supplier_options").parameters["required"]) == {"product_id"}
    assert set(_tool("search_rules").parameters["required"]) == {"query"}
    assert _tool("list_warehouses").parameters.get("required", []) == []
