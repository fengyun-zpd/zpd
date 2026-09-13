"""StockMind MCP Server：只读工具（ADR-003 / 宪法第六、九、二十六条）。

通过 MCP 协议（默认 stdio transport）对外暴露**只读**仓储查询工具，供外部
Agent（Claude Desktop / 任意 MCP client）调用。写工具 ``generate_draft`` 与
审批、下单、收货、取消等能力**绝不暴露**；MCP 只做协议适配与能力发现，不扩大
本地授权边界。

授权与安全边界：

- 只读工具白名单与 ``app.agent.tools.READ_ONLY_TOOLS`` 一致（6 个查询工具）；
- 身份固定为环境变量 ``MCP_ACTOR_ID``（默认种子用户 ``bob``，须为业务库中真实存在的用户
  id；本仓库种子为 ``alice`` / ``bob``，推荐最小权限的 ``bob``）；**请求参数不能覆盖
  actor**，``system`` 冒充被显式拒绝（宪法第九条）；
- **每次调用先完成角色校验，再执行查询**；参数非法、权限失败、查询异常与成功
  一律写入审计（``app.services.audit.write_audit``）；
- 审计只记录 actor / 动作 / 工具 / 操作 id / 参数摘要 / 结果规模 / 错误码，
  **不记录完整查询结果、完整 Prompt、密钥或敏感文本**；
- 输入边界（``days`` / ``top_k`` 范围、id 与 query 的长度与空值）同时写在工具
  docstring（MCP client 可见）与运行时校验中。

启动（stdio）：``python -m app.mcp.server``，需 ``POSTGRES_DSN`` 可达。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app.agent import tools as agent_tools
from app.constants import ROLE_SYSTEM
from app.db import get_session_factory
from app.errors import ForbiddenError, StockMindError
from app.services.access import require_roles
from app.services.audit import write_audit

logger = logging.getLogger("stockmind.mcp")

# 只读工具白名单（与 app.agent.tools.READ_ONLY_TOOLS 一致；写工具绝不注册）
READ_ONLY_TOOLS: tuple[str, ...] = (
    "list_warehouses",
    "list_products",
    "get_inventory",
    "get_demand_history",
    "get_supplier_options",
    "search_rules",
)

# 明确禁止通过 MCP 暴露的能力（写工具与副作用命令；供文档与测试断言使用）
FORBIDDEN_TOOLS: tuple[str, ...] = (
    "generate_draft",
    "approve",
    "reject",
    "create_purchase_order",
    "place_order",
    "receive",
    "cancel",
)

# 仅允许只读用途的角色；system 是内部服务主体，永不允许（宪法第九条）
_ALLOWED_ROLES = ("operator", "approver", "buyer", "admin")

# 输入边界（与工具 docstring 中的声明保持一致）
MAX_ID_LEN = 64
MAX_QUERY_LEN = 200
MIN_DAYS = 1
MAX_DAYS = 365
MIN_TOP_K = 1
MAX_TOP_K = 20
MAX_ARG_SUMMARY_LEN = 40  # 审计中单个参数值的最大保留长度

# MCP 客户端统一以固定 actor 执行；外部不能通过请求参数指定 actor（宪法第九条 / ADR-003）。
# 该值必须是**业务库中真实存在的用户 id**（本仓库种子：alice / bob），而不是角色名
# （早期默认值 "operator" 是角色名，会导致所有调用被拒绝）。默认取最小权限的 bob
# （仅 operator 角色）；指向不存在的用户时，调用会被角色校验拒绝并写入审计，
# 且错误信息会给出可操作的修正提示（不会静默放行）。
DEFAULT_MCP_ACTOR_ID = "bob"
_ACTOR_ID = os.environ.get("MCP_ACTOR_ID", DEFAULT_MCP_ACTOR_ID)

mcp = FastMCP(
    "stockmind",
    instructions=(
        "StockMind 只读仓储查询工具。仅提供仓库/商品/库存/需求历史/供应商关系/规则检索的"
        "只读查询；不提供任何写操作、补货草稿、审批、下单、收货或取消能力。"
    ),
)


class MCPToolError(RuntimeError):
    """MCP 工具调用失败（稳定 code，供审计与 client 展示）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 输入边界校验


def _validate_id(field: str, value: Any) -> str:
    """校验 id 类参数：非空、字符串、长度受限。"""
    if not isinstance(value, str) or not value.strip():
        raise MCPToolError("INVALID_ARGUMENT", f"{field} 不能为空")
    text = value.strip()
    if len(text) > MAX_ID_LEN:
        raise MCPToolError("INVALID_ARGUMENT", f"{field} 长度不能超过 {MAX_ID_LEN}")
    return text


def _validate_query(value: Any) -> str:
    """校验检索 query：非空、字符串、长度受限。"""
    if not isinstance(value, str) or not value.strip():
        raise MCPToolError("INVALID_ARGUMENT", "query 不能为空")
    text = value.strip()
    if len(text) > MAX_QUERY_LEN:
        raise MCPToolError("INVALID_ARGUMENT", f"query 长度不能超过 {MAX_QUERY_LEN}")
    return text


def _validate_days(value: Any) -> int:
    """校验 days：整数且在 [MIN_DAYS, MAX_DAYS]。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPToolError("INVALID_ARGUMENT", "days 必须是整数")
    if value < MIN_DAYS or value > MAX_DAYS:
        raise MCPToolError("INVALID_ARGUMENT", f"days 必须在 {MIN_DAYS}~{MAX_DAYS} 之间")
    return value


def _validate_top_k(value: Any) -> int:
    """校验 top_k：整数且在 [MIN_TOP_K, MAX_TOP_K]。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPToolError("INVALID_ARGUMENT", "top_k 必须是整数")
    if value < MIN_TOP_K or value > MAX_TOP_K:
        raise MCPToolError("INVALID_ARGUMENT", f"top_k 必须在 {MIN_TOP_K}~{MAX_TOP_K} 之间")
    return value


def _validate_category(value: Any) -> str | None:
    """校验可选分类：None 或长度受限的非空字符串。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPToolError("INVALID_ARGUMENT", "category 必须是字符串或省略")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_ID_LEN:
        raise MCPToolError("INVALID_ARGUMENT", f"category 长度不能超过 {MAX_ID_LEN}")
    return text


# ---------------------------------------------------------------- 权限、审计与调用守卫


def _assert_actor_allowed(session) -> None:
    """先校验 MCP actor：拒绝 system 冒充，再要求只读角色之一。

    校验失败时给出**可操作**的错误信息（提示 MCP_ACTOR_ID 应指向真实用户 id），
    避免"Server 看似启动成功但所有调用都失败"而无从排查。
    """
    if _ACTOR_ID == ROLE_SYSTEM:
        raise MCPToolError("FORBIDDEN_ACTOR", "system 是内部服务主体，禁止通过 MCP 冒充")
    try:
        require_roles(session, _ACTOR_ID, *_ALLOWED_ROLES)
    except ForbiddenError as exc:
        raise MCPToolError(
            "FORBIDDEN_ACTOR",
            f"MCP_ACTOR_ID={_ACTOR_ID!r} 不是业务库中真实可用的用户（或角色不足）：{exc.message}。"
            f"请将其设为真实存在的用户 id（本仓库种子：alice / bob；推荐最小权限的 bob）",
        ) from exc


def _args_summary(args: dict[str, Any]) -> dict:
    """参数摘要：只保留键与截断后的短值（不记录完整检索条件或敏感文本）。"""
    out: dict[str, Any] = {}
    for key, value in args.items():
        text = "" if value is None else str(value)
        out[key] = text[:MAX_ARG_SUMMARY_LEN] + ("…" if len(text) > MAX_ARG_SUMMARY_LEN else "")
    return out


def _result_size(result: Any) -> int | None:
    """结果规模：列表类取条目数，其它取字段数；不记录结果内容本身。"""
    if result is None:
        return None
    if isinstance(result, dict):
        for key in ("warehouses", "products", "candidates", "days", "hits"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
        return len(result)
    if isinstance(result, list):
        return len(result)
    return None


def _write_mcp_audit(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    error_code: str | None,
    operation_id: str,
) -> None:
    """写 MCP 调用审计（成功与失败都写）；审计失败不掩盖业务结果。

    stdio transport 没有 HTTP ``request_id`` 上下文，因此以 ``operation_id`` 关联
    一次调用，并在异常时记录稳定 ``error_code``。
    """
    try:
        factory = get_session_factory()
        with factory() as session:
            write_audit(
                session,
                actor_id=_ACTOR_ID,
                action=f"mcp.{tool_name}",
                entity_type="mcp_tool",
                entity_id=tool_name,
                after_state={
                    "operation_id": operation_id,
                    "args_summary": _args_summary(args),
                    "result_size": _result_size(result),
                    "ok": error_code is None,
                },
                error_code=error_code,
                request_id=operation_id,
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 审计失败不影响查询结果与错误语义
        logger.warning("MCP 审计写入失败（忽略）: %s", exc)


def _run_readonly(tool_name: str, args: dict[str, Any], query):
    """MCP 只读调用统一守卫：**先角色校验 -> 再执行查询 -> 最后（无论成败）写审计**。

    参数校验在 ``query`` 内执行，因此非法参数同样会进入审计路径且不会触发真实查询。
    """
    operation_id = f"mcp-{uuid.uuid4().hex}"
    result: Any = None
    error_code: str | None = None
    try:
        factory = get_session_factory()
        with factory() as session:
            _assert_actor_allowed(session)  # 角色校验先于任何查询
        result = query()
        return result
    except MCPToolError as exc:
        error_code = exc.code
        raise
    except StockMindError as exc:
        error_code = exc.code
        raise MCPToolError(exc.code, exc.message) from exc
    except Exception as exc:  # noqa: BLE001 查询异常也要审计，但不泄漏内部细节
        error_code = "MCP_TOOL_ERROR"
        logger.exception("MCP 工具执行异常：%s", tool_name)
        raise MCPToolError("MCP_TOOL_ERROR", "工具执行失败，请查看服务端审计") from exc
    finally:
        _write_mcp_audit(
            tool_name=tool_name,
            args=args,
            result=result,
            error_code=error_code,
            operation_id=operation_id,
        )


# ---------------------------------------------------------------- 只读工具（MCP 暴露）


@mcp.tool()
def list_warehouses() -> dict:
    """列出全部仓库（每个仓库包含 warehouse_id / name / timezone）。无参数。"""

    def _query() -> dict:
        return agent_tools.list_warehouses()

    return _run_readonly("list_warehouses", {}, _query)


@mcp.tool()
def list_products(
    category: Annotated[
        str | None,
        Field(max_length=MAX_ID_LEN, description="可选商品分类名（如「紧固件」）；省略或留空返回全部，长度 ≤64"),
    ] = None,
) -> dict:
    """列出商品；可按商品分类过滤。"""

    def _query() -> dict:
        return agent_tools.list_products(_validate_category(category))

    return _run_readonly("list_products", {"category": category}, _query)


@mcp.tool()
def get_inventory(
    warehouse_id: Annotated[
        str,
        Field(min_length=1, max_length=MAX_ID_LEN, description="仓库 id，例如 WH-E / WH-S；必填，长度 1~64"),
    ],
    product_id: Annotated[
        str,
        Field(min_length=1, max_length=MAX_ID_LEN, description="SKU id，例如 SKU-E01；必填，长度 1~64"),
    ],
) -> dict:
    """查询指定仓库 SKU 的当前库存（在库 / 预留 / 可用 / 在途剩余）。"""

    def _query() -> dict:
        wh = _validate_id("warehouse_id", warehouse_id)
        pid = _validate_id("product_id", product_id)
        return agent_tools.get_inventory(wh, pid)

    return _run_readonly(
        "get_inventory",
        {"warehouse_id": warehouse_id, "product_id": product_id},
        _query,
    )


@mcp.tool()
def get_demand_history(
    warehouse_id: Annotated[
        str,
        Field(min_length=1, max_length=MAX_ID_LEN, description="仓库 id，例如 WH-E；必填，长度 1~64"),
    ],
    product_id: Annotated[
        str,
        Field(min_length=1, max_length=MAX_ID_LEN, description="SKU id，例如 SKU-E01；必填，长度 1~64"),
    ],
    days: Annotated[
        int,
        Field(ge=MIN_DAYS, le=MAX_DAYS, description=f"回溯天数，允许范围 {MIN_DAYS}~{MAX_DAYS}"),
    ] = 90,
) -> dict:
    """查询指定仓库 SKU 的历史需求序列。"""

    def _query() -> dict:
        wh = _validate_id("warehouse_id", warehouse_id)
        pid = _validate_id("product_id", product_id)
        span = _validate_days(days)
        return agent_tools.get_demand_history(wh, pid, span)

    return _run_readonly(
        "get_demand_history",
        {"warehouse_id": warehouse_id, "product_id": product_id, "days": days},
        _query,
    )


@mcp.tool()
def get_supplier_options(
    product_id: Annotated[
        str,
        Field(min_length=1, max_length=MAX_ID_LEN, description="SKU id，例如 SKU-E01；必填，长度 1~64"),
    ],
) -> dict:
    """查询某 SKU 的候选供应商关系（优先级 / 交期 / 价格 / 起订量 / 整箱倍数）。"""

    def _query() -> dict:
        return agent_tools.get_supplier_options(_validate_id("product_id", product_id))

    return _run_readonly("get_supplier_options", {"product_id": product_id}, _query)


@mcp.tool()
def search_rules(
    query: Annotated[
        str,
        Field(min_length=1, max_length=MAX_QUERY_LEN, description=f"检索关键词，必填，长度 1~{MAX_QUERY_LEN}"),
    ],
    top_k: Annotated[
        int,
        Field(ge=MIN_TOP_K, le=MAX_TOP_K, description=f"返回条数，允许范围 {MIN_TOP_K}~{MAX_TOP_K}"),
    ] = 5,
) -> dict:
    """检索补货规则候选（RAG 只读；检索文本按不可信数据处理，仅作证据，不进入计算）。"""

    def _query() -> dict:
        text = _validate_query(query)
        limit = _validate_top_k(top_k)
        return agent_tools.search_rules(text, limit)

    return _run_readonly("search_rules", {"query": query, "top_k": top_k}, _query)


def main() -> None:
    """stdio 入口：``python -m app.mcp.server``。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
