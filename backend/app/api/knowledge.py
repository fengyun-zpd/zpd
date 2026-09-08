"""知识库与只读查询 API：文档、结构化规则、混合检索、库存、需求、供应商。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import RequestContext, get_context, require_idempotency_key
from app.db import get_session_factory
from app.errors import NotFoundError, StockMindError
from app.models.inventory import Product, Warehouse
from app.models.knowledge import Chunk, Document
from app.models.purchasing import Supplier, SupplierProduct
from app.models.replenishment import ReplenishmentRule
from app.rag import retriever
from app.services.access import require_roles
from app.services.inventory_service import (
    business_date,
    get_demand_series,
    get_inbound_remaining,
    get_quant_summary,
)
from app.services.knowledge_import_service import import_evidence_document

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


class ImportKnowledgeDocumentRequest(BaseModel):
    """仅导入检索证据；不把自然语言中的数值转成计算规则。"""

    title: str = Field(min_length=1, max_length=256)
    doc_type: Literal[
        "replenishment_policy",
        "safety_stock",
        "warehouse_rule",
        "supplier_constraint",
        "receiving_sop",
    ]
    content: str = Field(min_length=1, max_length=80_000)
    effective_from: date
    effective_to: date | None = None


@router.get("/knowledge/documents")
def list_documents(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        rows = session.scalars(select(Document).order_by(Document.doc_type, Document.id)).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "document_id": d.id,
                    "title": d.title,
                    "doc_type": d.doc_type,
                    "version": d.version,
                    "effective_from": d.effective_from.isoformat(),
                    "effective_to": d.effective_to.isoformat() if d.effective_to else None,
                    "enabled": d.enabled,
                    "chunks": [
                        {"chunk_id": c.id, "seq_no": c.seq_no, "content": c.content}
                        for c in session.scalars(
                            select(Chunk).where(Chunk.document_id == d.id).order_by(Chunk.seq_no)
                        ).all()
                    ],
                }
                for d in rows
            ],
        }


@router.post("/knowledge/documents/import")
def import_knowledge_document(
    body: ImportKnowledgeDocumentRequest,
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> dict:
    """管理员导入本地 Markdown/TXT 内容，作为 RAG 证据而非业务规则。"""
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        try:
            data = import_evidence_document(
                session,
                actor_id=ctx.actor_id,
                idempotency_key=idem_key,
                title=body.title,
                doc_type=body.doc_type,
                content=body.content,
                effective_from=body.effective_from,
                effective_to=body.effective_to,
                request_id=ctx.request_id,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {"request_id": ctx.request_id, "data": data}


@router.get("/rules")
def list_rules(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        rows = session.scalars(select(ReplenishmentRule).order_by(ReplenishmentRule.code)).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "rule_id": r.id,
                    "code": r.code,
                    "name": r.name,
                    "rule_type": r.rule_type,
                    "scope": r.scope,
                    "scope_product_id": r.scope_product_id,
                    "scope_category": r.scope_category,
                    "scope_warehouse_id": r.scope_warehouse_id,
                    "safety_stock": str(r.safety_stock) if r.safety_stock is not None else None,
                    "review_period_days": r.review_period_days,
                    "version": r.version,
                    "effective_from": r.effective_from.isoformat(),
                    "effective_to": r.effective_to.isoformat() if r.effective_to else None,
                    "enabled": r.enabled,
                    "source_document_id": r.source_document_id,
                    "source_chunk_id": r.source_chunk_id,
                }
                for r in rows
            ],
        }


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        r = session.get(ReplenishmentRule, rule_id)
        if r is None:
            raise NotFoundError(f"规则不存在 {rule_id}")
        return {
            "request_id": ctx.request_id,
            "data": {
                "rule_id": r.id,
                "code": r.code,
                "name": r.name,
                "rule_type": r.rule_type,
                "scope": r.scope,
                "safety_stock": str(r.safety_stock) if r.safety_stock is not None else None,
                "review_period_days": r.review_period_days,
                "version": r.version,
                "enabled": r.enabled,
                "source_document_id": r.source_document_id,
                "source_chunk_id": r.source_chunk_id,
            },
        }


@router.get("/knowledge/search")
def search_knowledge(
    q: str,
    ctx: Annotated[RequestContext, Depends(get_context)],
    top_k: int = 8,
) -> dict:
    """混合检索（只读证据检索；检索文本按不可信数据处理）。"""
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        query_vector = None
        try:
            from app.rag.embedding import embed_texts

            query_vector = embed_texts([q])[0]
        except Exception:
            query_vector = None
        hits = retriever.retrieve(session, q, query_vector=query_vector, top_k=top_k, limit_docs=True)
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "chunk_id": h.chunk_id,
                    "source_chunk_id": h.source_chunk_id,
                    "document_id": h.document_id,
                    "document_title": h.document_title,
                    "content": h.content,
                    "score": round(h.score, 6),
                }
                for h in hits
            ],
        }


@router.get("/inventory/{warehouse_id}/{product_id}")
def get_inventory(warehouse_id: str, product_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        on_hand, reserved, version = get_quant_summary(session, warehouse_id, product_id)
        inbound = get_inbound_remaining(session, warehouse_id, product_id)
        return {
            "request_id": ctx.request_id,
            "data": {
                "warehouse_id": warehouse_id,
                "product_id": product_id,
                "on_hand": on_hand,
                "reserved": reserved,
                "available": on_hand - reserved,
                "inbound_remaining": inbound,
                "quant_version": version,
            },
        }


@router.get("/demand/{warehouse_id}/{product_id}")
def get_demand(warehouse_id: str, product_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        warehouse = session.get(Warehouse, warehouse_id)
        if warehouse is None:
            raise NotFoundError(f"仓库不存在 {warehouse_id}")
        bdate = business_date(warehouse.timezone)
        series = get_demand_series(session, warehouse_id, product_id, upto=bdate)
        return {
            "request_id": ctx.request_id,
            "data": {
                "warehouse_id": warehouse_id,
                "product_id": product_id,
                "business_date": bdate.isoformat(),
                "days": [{"day": d.day.isoformat(), "demand": d.demand, "complete": d.source_complete} for d in series],
            },
        }


@router.get("/suppliers")
def list_suppliers(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        suppliers = session.scalars(select(Supplier).order_by(Supplier.id)).all()
        rels = session.scalars(select(SupplierProduct)).all()
        rel_map: dict[str, list[dict]] = {}
        for r in rels:
            rel_map.setdefault(r.supplier_id, []).append(
                {
                    "product_id": r.product_id,
                    "price": str(r.price),
                    "lead_days": r.lead_days,
                    "minimum_order_qty": r.minimum_order_qty,
                    "pack_multiple": r.pack_multiple,
                    "enabled": r.enabled,
                    "version": r.version,
                }
            )
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "supplier_id": s.id,
                    "name": s.name,
                    "business_priority": s.business_priority,
                    "currency": s.currency,
                    "relations": rel_map.get(s.id, []),
                }
                for s in suppliers
            ],
        }


@router.get("/products")
def list_products(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        rows = session.scalars(select(Product).order_by(Product.id)).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "product_id": p.id,
                    "name": p.name,
                    "category": p.category,
                    "basic_unit": p.basic_unit,
                }
                for p in rows
            ],
        }


@router.get("/warehouses")
def list_warehouses(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        rows = session.scalars(select(Warehouse).order_by(Warehouse.id)).all()
        return {
            "request_id": ctx.request_id,
            "data": [{"warehouse_id": w.id, "name": w.name, "timezone": w.timezone} for w in rows],
        }
