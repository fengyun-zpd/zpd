"""通过官方 MCP Python SDK 验证 StockMind stdio Server 的协议互操作性。

该脚本只验证协议握手、工具白名单和一次只读调用；不会创建草稿，也不会触发业务副作用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_ONLY_TOOLS = {
    "list_warehouses",
    "list_products",
    "get_inventory",
    "get_demand_history",
    "get_supplier_options",
    "search_rules",
}
FORBIDDEN_TOOLS = {
    "generate_draft",
    "approve_plan",
    "create_purchase_order",
    "place_order",
}


def _server_params(backend_root: Path, python: str, actor_id: str) -> StdioServerParameters:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(backend_root),
            "MCP_ACTOR_ID": actor_id,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return StdioServerParameters(
        command=python,
        args=["-m", "app.mcp.server"],
        cwd=backend_root,
        env=env,
    )


async def verify(backend_root: Path, python: str, actor_id: str) -> dict[str, object]:
    async with stdio_client(_server_params(backend_root, python, actor_id)) as (read, write), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        result = await session.list_tools()
        names = {tool.name for tool in result.tools}
        if names != READ_ONLY_TOOLS:
            raise AssertionError(f"MCP 工具白名单不匹配: {sorted(names)}")
        if names & FORBIDDEN_TOOLS:
            raise AssertionError(f"MCP 暴露了禁止工具: {sorted(names & FORBIDDEN_TOOLS)}")

        call = await session.call_tool("list_warehouses", {})
        if getattr(call, "isError", False):
            raise AssertionError(f"只读工具调用失败: {call}")
        if not call.content:
            raise AssertionError("list_warehouses 返回空 MCP 内容")

        return {
            "transport": "stdio",
            "actor_id": actor_id,
            "tools": sorted(names),
            "readonly_call": "list_warehouses",
            "content_items": len(call.content),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor-id", default=os.environ.get("MCP_ACTOR_ID", "bob"))
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    backend_root = repo_root / "backend"
    result = asyncio.run(verify(backend_root, args.python, args.actor_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("MCP stdio protocol verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
