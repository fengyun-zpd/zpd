"""Agent 工具白名单（需求 3.2 / 架构 3.1 / 宪法第六、八、九条）。

只读工具：查询商品、仓库、库存、在途、历史需求、供应商关系、规则候选（RAG）。
受控写工具（唯一）：生成补货草稿——强类型参数，调用领域服务完成
权限、规则、数量、输入新鲜度、活动建议唯一性和幂等校验。

LLM 不可调用审批、建单、下单、查询恢复、收货、关闭/取消、修改规则、
定时任务配置和切换故障模式。所有工具调用记录到 tool_calls（不含敏感载荷）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.models.inventory import Product, Warehouse
from app.rag import retriever
from app.services import plan_service
from app.services.inventory_service import (
    business_date,
    get_demand_series,
    get_inbound_remaining,
    get_quant_summary,
)

logger = logging.getLogger("stockmind.agent.tools")

# 工具白名单（供评测断言与文档核对使用）
TOOL_WHITELIST = {
    "list_warehouses",
    "list_products",
    "get_inventory",
    "get_demand_history",
    "get_supplier_options",
    "search_rules",
    "generate_draft",
}


def _session() -> Session:
    factory = get_session_factory()
    return factory()


def record(tool_calls: list[dict], name: str, args: dict, result: dict | None, error: str | None = None) -> None:
    """工具审计记录（不保存完整 Prompt 与密钥）。"""
    tool_calls.append(
        {
            "tool": name,
            "args": args,
            "ok": error is None,
            "error": error,
            "result_summary": _summary(result),
        }
    )


def _summary(result: dict | None) -> str | None:
    if result is None:
        return None
    text = str(result)
    return text[:200] + ("…" if len(text) > 200 else "")


# ---------------------------------------------------------------- 只读工具


def list_warehouses() -> dict:
    with _session() as session:
        rows = session.scalars(select(Warehouse).order_by(Warehouse.id)).all()
        return {"warehouses": [{"warehouse_id": w.id, "name": w.name, "timezone": w.timezone} for w in rows]}


def list_products(category: str | None = None) -> dict:
    with _session() as session:
        stmt = select(Product).order_by(Product.id)
        if category:
            stmt = stmt.where(Product.category == category)
        rows = session.scalars(stmt).all()
        return {"products": [{"product_id": p.id, "name": p.name, "category": p.category} for p in rows]}


def get_inventory(warehouse_id: str, product_id: str) -> dict:
    with _session() as session:
        on_hand, reserved, version = get_quant_summary(session, warehouse_id, product_id)
        inbound = get_inbound_remaining(session, warehouse_id, product_id)
        return {
            "warehouse_id": warehouse_id,
            "product_id": product_id,
            "on_hand": on_hand,
            "reserved": reserved,
            "available": on_hand - reserved,
            "inbound_remaining": inbound,
            "quant_version": version,
        }


def get_demand_history(warehouse_id: str, product_id: str, days: int = 90) -> dict:
    with _session() as session:
        warehouse = session.get(Warehouse, warehouse_id)
        if warehouse is None:
            return {"error": "warehouse not found"}
        bdate = business_date(warehouse.timezone)
        series = get_demand_series(session, warehouse_id, product_id, upto=bdate, lookback_days=days)
        return {
            "warehouse_id": warehouse_id,
            "product_id": product_id,
            "business_date": bdate.isoformat(),
            "days": [{"day": d.day.isoformat(), "demand": d.demand, "complete": d.source_complete} for d in series],
        }


def get_supplier_options(product_id: str) -> dict:
    with _session() as session:
        from app.models.purchasing import SupplierProduct

        rels = session.scalars(select(SupplierProduct).where(SupplierProduct.product_id == product_id)).all()
        return {
            "product_id": product_id,
            "candidates": [
                {
                    "supplier_id": r.supplier_id,
                    "business_priority": r.business_priority,
                    "lead_days": r.lead_days,
                    "price": str(r.price),
                    "minimum_order_qty": r.minimum_order_qty,
                    "pack_multiple": r.pack_multiple,
                    "enabled": r.enabled,
                }
                for r in rels
            ],
        }


def search_rules(query: str, top_k: int = 5) -> dict:
    """RAG 规则候选检索（只读；检索文本按不可信数据处理）。"""
    with _session() as session:
        query_vector = None
        try:
            from app.rag.embedding import embed_texts

            query_vector = embed_texts([query])[0]
        except Exception:
            query_vector = None
        hits = retriever.retrieve(session, query, query_vector=query_vector, top_k=top_k)
        return {
            "hits": [
                {
                    "source_chunk_id": h.source_chunk_id,
                    "document_id": h.document_id,
                    "document_title": h.document_title,
                    "content": h.content,
                }
                for h in hits
            ]
        }


# ---------------------------------------------------------------- 受控草稿工具（唯一写工具）


def generate_draft(
    *,
    actor_id: str,
    warehouse_id: str,
    products: list[str],
    requested_window: int,
    thread_id: str | None = None,
    operation_id: str | None = None,
) -> dict:
    """生成补货草稿（受控）：强类型参数，领域服务执行全部校验。

    LLM 不得覆盖供应商选择、补货数量、阻断结果或领域错误。
    """
    op_id = operation_id or f"agent:{actor_id}:{uuid.uuid4().hex}"
    with _session() as session:
        result = plan_service.generate_draft(
            session,
            warehouse_id=warehouse_id,
            requested_window=requested_window,
            actor_id=actor_id,
            products=products,
            operation_id=op_id,
            thread_id=thread_id,
            trigger_type="manual",
        )
        session.commit()
        return {
            "plan_id": result.plan_id,
            "created": result.created,
            "lines": [
                {
                    "product_id": o.product_id,
                    "flag": o.flag,
                    "line_id": o.line_id,
                    "order_qty": o.order_qty,
                    "blocked_code": o.blocked_code,
                    "blocked_reason": o.blocked_reason,
                }
                for o in result.lines
            ],
        }


# ---------------------------------------------------------------- 工具分发（供图节点调用）

READ_ONLY_TOOLS: dict[str, Any] = {
    "list_warehouses": list_warehouses,
    "list_products": list_products,
    "get_inventory": get_inventory,
    "get_demand_history": get_demand_history,
    "get_supplier_options": get_supplier_options,
    "search_rules": search_rules,
}
