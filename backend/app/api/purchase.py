"""采购 API：采购单查询、下达、未知状态查询、收货、关闭、取消。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import RequestContext, get_context, require_idempotency_key
from app.db import get_session_factory
from app.errors import NotFoundError, StockMindError
from app.models.purchasing import PurchaseOrder, PurchaseOrderAttempt, PurchaseOrderLine
from app.services import purchase_service
from app.services.access import require_roles
from app.supplier_adapter import HttpSupplierAdapter

router = APIRouter(prefix="/api/v1", tags=["purchase"])


class PlaceOrderRequest(BaseModel):
    pass


class QueryOrderRequest(BaseModel):
    pass


class ReceiveRequest(BaseModel):
    receipt_event_id: str
    qty: int = Field(gt=0)


def _po_dict(po: PurchaseOrder, *, with_lines: bool = False, with_attempts: bool = False) -> dict[str, object]:
    data: dict[str, object] = {
        "purchase_order_id": po.id,
        "warehouse_id": po.warehouse_id,
        "plan_id": po.plan_id,
        "supplier_id": po.supplier_id,
        "status": po.status,
        "currency": po.currency,
        "version": po.version,
        "created_at": po.created_at.isoformat() if po.created_at else None,
    }
    if with_lines:
        data["lines"] = []
    if with_attempts:
        data["attempts"] = []
    return data


def _po_detail(session: Session, po: PurchaseOrder) -> dict:
    data = _po_dict(po, with_lines=True, with_attempts=True)
    lines = session.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)).all()
    data["lines"] = [
        {
            "line_id": line.id,
            "product_id": line.product_id,
            "order_qty": line.order_qty,
            "received_qty": line.received_qty,
            "remaining": line.order_qty - line.received_qty,
            "unit_price": str(line.unit_price),
            "plan_line_id": line.plan_line_id,
        }
        for line in lines
    ]
    attempts = session.scalars(
        select(PurchaseOrderAttempt)
        .where(PurchaseOrderAttempt.purchase_order_id == po.id)
        .order_by(PurchaseOrderAttempt.attempt_no)
    ).all()
    data["attempts"] = [
        {
            "attempt_no": a.attempt_no,
            "status": a.status,
            "external_order_no": a.external_order_no,
            "supplier_idempotency_key": a.supplier_idempotency_key,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in attempts
    ]
    return data


@router.get("/purchase-orders")
def list_purchase_orders(
    ctx: Annotated[RequestContext, Depends(get_context)],
    status: str | None = None,
    warehouse_id: str | None = None,
) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        stmt = select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()).limit(100)
        if status:
            stmt = stmt.where(PurchaseOrder.status == status)
        if warehouse_id:
            stmt = stmt.where(PurchaseOrder.warehouse_id == warehouse_id)
        return {
            "request_id": ctx.request_id,
            "data": [_po_dict(p) for p in session.scalars(stmt).all()],
        }


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        po = session.get(PurchaseOrder, po_id)
        if po is None:
            raise NotFoundError(f"采购单不存在 {po_id}")
        return {"request_id": ctx.request_id, "data": _po_detail(session, po)}


@router.post("/purchase-orders/{po_id}/place")
def place_order(po_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "buyer", "admin")
        try:
            outcome = purchase_service.place_order(
                session,
                po_id=po_id,
                actor_id=ctx.actor_id,
                idempotency_key=idem_key,
                adapter=HttpSupplierAdapter(),
                request_id=ctx.request_id,
            )
        except StockMindError:
            session.rollback()
            raise
        return {"request_id": ctx.request_id, "data": outcome}


@router.post("/purchase-orders/{po_id}/query")
def query_order(po_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    """buyer 手动查询未知订单（system 后台恢复走 Celery 任务）。"""
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "buyer", "admin")
        try:
            outcome = purchase_service.query_unknown_order(
                session,
                po_id=po_id,
                actor_id=ctx.actor_id,
                idempotency_key=idem_key,
                adapter=HttpSupplierAdapter(),
                request_id=ctx.request_id,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {"request_id": ctx.request_id, "data": outcome}


@router.post("/purchase-order-lines/{po_line_id}/receive")
def receive_goods(po_line_id: str, body: ReceiveRequest, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "buyer", "admin")
        try:
            outcome = purchase_service.receive_goods(
                session,
                po_line_id=po_line_id,
                receipt_event_id=body.receipt_event_id,
                qty=body.qty,
                actor_id=ctx.actor_id,
                idempotency_key=idem_key,
                request_id=ctx.request_id,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {"request_id": ctx.request_id, "data": outcome}


@router.post("/purchase-orders/{po_id}/close")
def close_order(po_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "buyer", "admin")
        try:
            outcome = purchase_service.close_purchase_order(
                session,
                po_id=po_id,
                actor_id=ctx.actor_id,
                idempotency_key=idem_key,
                request_id=ctx.request_id,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {"request_id": ctx.request_id, "data": outcome}


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_order(po_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "buyer", "admin")
        try:
            outcome = purchase_service.cancel_purchase_order(
                session,
                po_id=po_id,
                actor_id=ctx.actor_id,
                idempotency_key=idem_key,
                request_id=ctx.request_id,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {"request_id": ctx.request_id, "data": outcome}
