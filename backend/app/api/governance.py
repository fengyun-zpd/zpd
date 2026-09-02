"""治理 API：定时任务、执行记录、告警、幂等操作、审计、演示用户、故障注入。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import RequestContext, get_context, require_idempotency_key
from app.db import get_session_factory
from app.errors import NotFoundError
from app.models.governance import (
    Alert,
    AuditLog,
    Execution,
    ExecutionItem,
    IdempotencyOperation,
    Schedule,
    User,
)
from app.services.access import require_roles
from app.supplier_adapter import HttpSupplierAdapter

router = APIRouter(prefix="/api/v1", tags=["governance"])


class ScheduleRequest(BaseModel):
    name: str
    cron_expr: str
    timezone: str = "Asia/Shanghai"
    enabled: bool = True
    default_window: int = Field(default=14, ge=7, le=30)


class FaultModeRequest(BaseModel):
    mode: str  # normal / explicit_failure / timeout_but_created


# ---------------------------------------------------------------- 定时任务


@router.get("/schedules")
def list_schedules(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "buyer", "operator", "approver")
        rows = session.scalars(select(Schedule).order_by(Schedule.created_at)).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "schedule_id": s.id,
                    "name": s.name,
                    "cron_expr": s.cron_expr,
                    "timezone": s.timezone,
                    "enabled": s.enabled,
                    "default_window": s.default_window,
                }
                for s in rows
            ],
        }


@router.post("/schedules")
def create_schedule(body: ScheduleRequest, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin")
        s = Schedule(
            name=body.name,
            cron_expr=body.cron_expr,
            timezone=body.timezone,
            enabled=body.enabled,
            default_window=body.default_window,
            created_by=ctx.actor_id,
        )
        session.add(s)
        session.commit()
        return {"request_id": ctx.request_id, "data": {"schedule_id": s.id}}


@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: str, body: ScheduleRequest, ctx: Annotated[RequestContext, Depends(get_context)]
) -> dict:
    require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin")
        s = session.get(Schedule, schedule_id)
        if s is None:
            raise NotFoundError(f"定时任务不存在 {schedule_id}")
        s.name = body.name
        s.cron_expr = body.cron_expr
        s.timezone = body.timezone
        s.enabled = body.enabled
        s.default_window = body.default_window
        session.commit()
        return {"request_id": ctx.request_id, "data": {"schedule_id": s.id}}


@router.post("/schedules/{schedule_id}/run")
def run_schedule_now(schedule_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    """立即执行：admin 触发，执行主体记为 system，另存 triggered_by_actor_id。"""
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin")
        s = session.get(Schedule, schedule_id)
        if s is None:
            raise NotFoundError(f"定时任务不存在 {schedule_id}")
        from app.services.execution_service import run_scan_for_schedule

        execution_id = run_scan_for_schedule(
            schedule_id=s.id,
            scheduled_at=None,  # 立即执行：生成新执行记录
            trigger_type="manual",
            triggered_by_actor_id=ctx.actor_id,
            idempotency_key=idem_key,
        )
        return {"request_id": ctx.request_id, "data": {"execution_id": execution_id}}


@router.post("/executions/{execution_id}/rerun")
def rerun_execution(execution_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    """手动重跑：admin 触发，创建带来源引用的新执行记录。"""
    idem_key = require_idempotency_key(ctx)
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin")
        from app.services.execution_service import rerun_execution as _rerun

        new_id = _rerun(
            execution_id=execution_id,
            triggered_by_actor_id=ctx.actor_id,
            idempotency_key=idem_key,
        )
        return {"request_id": ctx.request_id, "data": {"execution_id": new_id}}


# ---------------------------------------------------------------- 执行记录 / 告警


@router.get("/executions")
def list_executions(
    ctx: Annotated[RequestContext, Depends(get_context)],
    schedule_id: str | None = None,
    status: str | None = None,
) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "operator", "approver", "buyer")
        stmt = select(Execution).order_by(Execution.started_at.desc()).limit(100)
        if schedule_id:
            stmt = stmt.where(Execution.schedule_id == schedule_id)
        if status:
            stmt = stmt.where(Execution.status == status)
        rows = session.scalars(stmt).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "execution_id": e.id,
                    "schedule_id": e.schedule_id,
                    "trigger_type": e.trigger_type,
                    "scheduled_at": e.scheduled_at.isoformat(),
                    "started_at": e.started_at.isoformat(),
                    "status": e.status,
                    "scanned_count": e.scanned_count,
                    "draft_count": e.draft_count,
                    "success_count": e.success_count,
                    "blocked_count": e.blocked_count,
                    "failed_count": e.failed_count,
                    "error_summary": e.error_summary,
                    "triggered_by_actor_id": e.triggered_by_actor_id,
                }
                for e in rows
            ],
        }


@router.get("/executions/{execution_id}")
def get_execution(execution_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "operator", "approver", "buyer")
        e = session.get(Execution, execution_id)
        if e is None:
            raise NotFoundError(f"执行记录不存在 {execution_id}")
        items = session.scalars(select(ExecutionItem).where(ExecutionItem.execution_id == execution_id)).all()
        return {
            "request_id": ctx.request_id,
            "data": {
                "execution_id": e.id,
                "schedule_id": e.schedule_id,
                "trigger_type": e.trigger_type,
                "scheduled_at": e.scheduled_at.isoformat(),
                "started_at": e.started_at.isoformat(),
                "status": e.status,
                "scanned_count": e.scanned_count,
                "draft_count": e.draft_count,
                "success_count": e.success_count,
                "blocked_count": e.blocked_count,
                "failed_count": e.failed_count,
                "error_summary": e.error_summary,
                "trace_id": e.trace_id,
                "triggered_by_actor_id": e.triggered_by_actor_id,
                "items": [
                    {
                        "warehouse_id": i.warehouse_id,
                        "product_id": i.product_id,
                        "status": i.status,
                        "error_code": i.error_code,
                        "block_reason": i.block_reason,
                        "plan_id": i.plan_id,
                        "plan_line_id": i.plan_line_id,
                        "alert_id": i.alert_id,
                    }
                    for i in items
                ],
            },
        }


@router.get("/alerts")
def list_alerts(
    ctx: Annotated[RequestContext, Depends(get_context)],
    status: str | None = "open",
) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "operator", "approver", "buyer")
        stmt = select(Alert).order_by(Alert.created_at.desc()).limit(100)
        if status:
            stmt = stmt.where(Alert.status == status)
        rows = session.scalars(stmt).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "alert_id": a.id,
                    "alert_type": a.alert_type,
                    "warehouse_id": a.warehouse_id,
                    "product_id": a.product_id,
                    "blocker_code": a.blocker_code,
                    "status": a.status,
                    "message": a.message,
                    "created_at": a.created_at.isoformat(),
                }
                for a in rows
            ],
        }


# ---------------------------------------------------------------- 幂等操作 / 审计


@router.get("/operations/{operation_id}")
def get_operation(operation_id: str, ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "operator", "approver", "buyer")
        op = session.get(IdempotencyOperation, operation_id)
        if op is None:
            raise NotFoundError(f"幂等操作不存在 {operation_id}")
        return {
            "request_id": ctx.request_id,
            "data": {
                "operation_id": op.id,
                "command_type": op.command_type,
                "aggregate_ref": op.aggregate_ref,
                "status": op.status,
                "payload_hash": op.payload_hash,
                "response": op.response,
                "entity_type": op.entity_type,
                "entity_id": op.entity_id,
                "updated_at": op.updated_at.isoformat() if op.updated_at else None,
            },
        }


@router.get("/audit-logs")
def list_audit_logs(
    ctx: Annotated[RequestContext, Depends(get_context)],
    actor_id: str | None = None,
    entity_type: str | None = None,
) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "buyer", "approver")
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        rows = session.scalars(stmt).all()
        return {
            "request_id": ctx.request_id,
            "data": [
                {
                    "log_id": a.id,
                    "actor_id": a.actor_id,
                    "action": a.action,
                    "entity_type": a.entity_type,
                    "entity_id": a.entity_id,
                    "error_code": a.error_code,
                    "before_state": a.before_state,
                    "after_state": a.after_state,
                    "created_at": a.created_at.isoformat(),
                }
                for a in rows
            ],
        }


# ---------------------------------------------------------------- 演示用户切换器


@router.get("/users")
def list_users(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        return {
            "request_id": ctx.request_id,
            "data": [{"actor_id": u.id, "display_name": u.display_name, "roles": list(u.roles)} for u in users],
        }


# ---------------------------------------------------------------- 故障模式（仅 admin）


@router.put("/admin/fault-modes/{supplier_id}")
def set_fault_mode(
    supplier_id: str, body: FaultModeRequest, ctx: Annotated[RequestContext, Depends(get_context)]
) -> dict:
    """切换模拟供应商故障模式：仅 admin，写入审计日志，不暴露为 Agent 工具。"""
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin")
        adapter = HttpSupplierAdapter()
        result = adapter.set_fault_mode(supplier_id, body.mode)
        from app.services.audit import write_audit

        write_audit(
            session,
            actor_id=ctx.actor_id,
            action="set_fault_mode",
            entity_type="supplier",
            entity_id=supplier_id,
            after_state={"mode": body.mode, "result": result},
            request_id=ctx.request_id,
        )
        session.commit()
        return {
            "request_id": ctx.request_id,
            "data": {"supplier_id": supplier_id, "mode": body.mode},
        }


@router.get("/fault-modes")
def list_fault_modes(ctx: Annotated[RequestContext, Depends(get_context)]) -> dict:
    factory = get_session_factory()
    with factory() as session:
        require_roles(session, ctx.actor_id, "admin", "buyer")
        adapter = HttpSupplierAdapter()
        modes = adapter.get_fault_modes()
        return {"request_id": ctx.request_id, "data": modes}
