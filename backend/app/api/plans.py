"""补货计划 API：草稿、查询、审批/排除/驳回、修订、按供应商建单。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select

from app.api.deps import RequestContext, get_context, require_idempotency_key
from app.db import get_session_factory
from app.errors import StockMindError
from app.models.purchasing import PurchaseOrder
from app.models.replenishment import PlanLine, ReplenishmentPlan
from app.services import plan_service
from app.services.access import require_roles

router = APIRouter(prefix="/api/v1", tags=["plans"])


class DraftRequest(BaseModel):
    warehouse_id: str
    requested_window: int = Field(ge=7, le=30)
    products: list[str] = Field(min_length=1)
    thread_id: str | None = None


class ApproveRequest(BaseModel):
    decisions: dict[str, str] = Field(default_factory=dict)  # {line_id: "approve"|"exclude"}
    expected_version: int | None = None


class SupersedeRequest(BaseModel):
    requested_window: int | None = None
    products: list[str] | None = None
    operation_id: str | None = None


class CreatePORequest(BaseModel):
    expected_version: int | None = None


def _line_dict(line: PlanLine) -> dict:
    return {
        "line_id": line.id,
        "product_id": line.product_id,
        "flag": line.flag,
        "active_for_dedupe": line.active_for_dedupe,
        "exclusion_reason": line.exclusion_reason,
        "blocked_code": line.blocked_code,
        "blocked_reason": line.blocked_reason,
        "order_qty": line.order_qty,
        "daily_forecast": str(line.daily_forecast) if line.daily_forecast is not None else None,
        "forecast_algorithm": line.forecast_algorithm,
        "mae": str(line.mae) if line.mae is not None else None,
        "wape": str(line.wape) if line.wape is not None else None,
        "fallback_reason": line.fallback_reason,
        "planning_window": line.planning_window,
        "coverage_demand": str(line.coverage_demand),
        "target_stock": str(line.target_stock),
        "available": str(line.available),
        "inbound": str(line.inbound),
        "net_demand": str(line.net_demand),
        "supplier_id": line.supplier_id,
        "supplier_reason": line.supplier_reason,
        "decision_input_hash": line.decision_input_hash,
        "intermediate": line.intermediate,
        "rule_refs": line.rule_refs,
    }


def _plan_dict(plan: ReplenishmentPlan, *, with_lines: bool = False) -> dict[str, object]:
    data: dict[str, object] = {
        "plan_id": plan.id,
        "warehouse_id": plan.warehouse_id,
        "thread_id": plan.thread_id,
        "actor_id": plan.actor_id,
        "trigger_type": plan.trigger_type,
        "status": plan.status,
        "requested_window": plan.requested_window,
        "planning_date": plan.planning_date.isoformat(),
        "revision_of_plan_id": plan.revision_of_plan_id,
        "decision_version": plan.decision_version,
        "version": plan.version,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }
    if with_lines:
        data["lines"] = []
    return data


@router.get("/plans")
def list_plans(
    ctx: Annotated[RequestContext, Depends(get_context)],
    status: str | None = None,
    warehouse_id: str | None = None,
) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        # 空计划不可审批/建单；过滤旧版本或并发缺陷遗留的空计划，详情接口仍保留审计可见性。
        stmt = (
            select(ReplenishmentPlan)
            .where(exists().where(PlanLine.plan_id == ReplenishmentPlan.id))
            .order_by(ReplenishmentPlan.created_at.desc())
            .limit(100)
        )
        if status:
            stmt = stmt.where(ReplenishmentPlan.status == status)
        if warehouse_id:
            stmt = stmt.where(ReplenishmentPlan.warehouse_id == warehouse_id)
        plans = session.scalars(stmt).all()
        plan_ids = [p.id for p in plans]
        # 每计划统计：可采购明细数（valid 且 order_qty>0）与无需采购明细数（valid 且 order_qty<=0）
        stats: dict[str, tuple[int, int]] = {}
        if plan_ids:
            rows = session.execute(
                select(
                    PlanLine.plan_id,
                    func.count().filter(PlanLine.flag == "valid", PlanLine.order_qty > 0),
                    func.count().filter(PlanLine.flag == "valid", PlanLine.order_qty <= 0),
                )
                .where(PlanLine.plan_id.in_(plan_ids))
                .group_by(PlanLine.plan_id)
            ).all()
            stats = {plan_id: (purchasable, zero) for plan_id, purchasable, zero in rows}
        po_plan_ids: set[str] = set()
        if plan_ids:
            po_plan_ids = {
                pid
                for pid in session.scalars(
                    select(PurchaseOrder.plan_id).where(PurchaseOrder.plan_id.in_(plan_ids))
                ).all()
                if pid is not None
            }
        data: list[dict] = []
        for p in plans:
            d = _plan_dict(p)
            purchasable, zero = stats.get(p.id, (0, 0))
            d["purchasable_count"] = purchasable
            d["zero_qty_count"] = zero
            d["has_po"] = p.id in po_plan_ids
            data.append(d)
        return {"request_id": ctx.request_id, "data": data}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        plan = session.get(ReplenishmentPlan, plan_id)
        if plan is None:
            from app.errors import NotFoundError

            raise NotFoundError(f"计划不存在 {plan_id}")
        lines = session.scalars(select(PlanLine).where(PlanLine.plan_id == plan_id).order_by(PlanLine.product_id)).all()
        data = _plan_dict(plan, with_lines=True)
        valid = [line for line in lines if line.flag == "valid"]
        data["purchasable_count"] = sum(1 for line in valid if line.order_qty > 0)
        data["zero_qty_count"] = sum(1 for line in valid if line.order_qty <= 0)
        data["has_po"] = (
            session.scalar(select(PurchaseOrder.id).where(PurchaseOrder.plan_id == plan_id).limit(1)) is not None
        )
        data["lines"] = [_line_dict(line) for line in lines]
        return {"request_id": ctx.request_id, "data": data}


@router.post("/drafts")
def create_draft(
    body: DraftRequest,
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> dict:
    """生成补货草稿并提交待审批（operator；受控草稿工具的 REST 等价入口）。"""
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        try:
            result = plan_service.generate_draft(
                session,
                warehouse_id=body.warehouse_id,
                requested_window=body.requested_window,
                actor_id=ctx.actor_id,
                products=body.products,
                operation_id=idem_key,
                thread_id=body.thread_id,
                trigger_type="manual",
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {
            "request_id": ctx.request_id,
            "data": {
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
                        "supplier_id": o.supplier_id,
                    }
                    for o in result.lines
                ],
            },
        }


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str, body: ApproveRequest, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "approver", "admin")
        try:
            plan = plan_service.decide_plan(
                session,
                plan_id=plan_id,
                actor_id=ctx.actor_id,
                mode="approve",
                decisions=body.decisions,
                expected_version=body.expected_version,
                idempotency_key=idem_key,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {
            "request_id": ctx.request_id,
            "data": {"plan_id": plan.id, "status": plan.status, "version": plan.version},
        }


@router.post("/plans/{plan_id}/reject")
def reject_plan(plan_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "approver", "admin")
        try:
            plan = plan_service.decide_plan(
                session,
                plan_id=plan_id,
                actor_id=ctx.actor_id,
                mode="reject",
                idempotency_key=idem_key,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {
            "request_id": ctx.request_id,
            "data": {"plan_id": plan.id, "status": plan.status, "version": plan.version},
        }


@router.post("/plans/{plan_id}/supersede")
def supersede_plan(plan_id: str, body: SupersedeRequest, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "operator", "approver", "buyer", "admin")
        try:
            new_plan = plan_service.supersede_plan(
                session,
                plan_id=plan_id,
                actor_id=ctx.actor_id,
                requested_window=body.requested_window,
                products=body.products,
                operation_id=body.operation_id or idem_key,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {
            "request_id": ctx.request_id,
            "data": {
                "plan_id": new_plan.id,
                "revision_of_plan_id": plan_id,
                "status": new_plan.status,
            },
        }


@router.post("/plans/{plan_id}/purchase-orders")
def create_purchase_orders(
    plan_id: str, body: CreatePORequest, ctx: Annotated[RequestContext, Depends(get_context)]
) -> dict:
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "buyer", "admin")
        try:
            created = plan_service.create_purchase_orders(
                session,
                plan_id=plan_id,
                actor_id=ctx.actor_id,
                expected_version=body.expected_version,
                idempotency_key=idem_key,
            )
            session.commit()
        except StockMindError:
            session.rollback()
            raise
        return {
            "request_id": ctx.request_id,
            "data": {"purchase_order_ids": [po.id for po in created]},
        }
