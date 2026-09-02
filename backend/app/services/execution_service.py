"""定时扫描执行服务（需求 2.2 / 架构 8 / ADR 决策 8）。

- 同一 schedule_id + scheduled_at 只能创建一个计划执行；
- 每个 SKU 使用独立保存点，单行失败不得回滚整个批次；
- 预期业务问题 -> blocked，未分类运行异常 -> failed，均产生/刷新去重告警；
- 没有任何 valid 明细时不创建空的待审批计划；
- 手动重跑创建带来源引用的新执行记录，受行级幂等键保护。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import (
    CMD_SCAN,
    EXEC_ITEM_BLOCKED,
    EXEC_ITEM_FAILED,
    EXEC_ITEM_SUCCESS,
    EXECUTION_FAILED,
    EXECUTION_PARTIAL,
    EXECUTION_RUNNING,
    EXECUTION_SUCCEEDED,
    LINE_BLOCKED,
    LINE_VALID,
    PLAN_PENDING_APPROVAL,
    TRIGGER_RERUN,
)
from app.errors import ConflictError
from app.models.governance import Execution, ExecutionItem, Schedule
from app.models.inventory import Product, Warehouse
from app.models.replenishment import PlanLine, ReplenishmentPlan
from app.services.alert_service import close_open_alert, upsert_open_alert
from app.services.audit import write_audit
from app.services.locks import acquire_warehouse_product_locks
from app.services.plan_service import LineOutcome, compute_line_for_sku

logger = logging.getLogger("stockmind.scan")


def _new_execution_id() -> str:
    return uuid.uuid4().hex


def run_scan(
    session: Session,
    *,
    schedule_id: str | None,
    scheduled_at: datetime | None,
    trigger_type: str,
    triggered_by_actor_id: str | None,
    idempotency_key: str,
    warehouse_ids: list[str] | None = None,
    product_ids: list[str] | None = None,
    requested_window: int | None = None,
    source_execution_id: str | None = None,
) -> str:
    """执行一次定时/手动/重跑扫描，返回 execution_id。"""
    # 1) 创建执行记录（幂等：同 schedule+time 或 同 schedule+幂等键 不得重复创建）
    now = datetime.now(timezone.utc)
    scheduled_at = scheduled_at or now
    execution = Execution(
        id=_new_execution_id(),
        schedule_id=schedule_id,
        trigger_type=trigger_type,
        scheduled_at=scheduled_at,
        timezone="Asia/Shanghai",
        started_at=now,
        status=EXECUTION_RUNNING,
        triggered_by_actor_id=triggered_by_actor_id,
        idempotency_key=idempotency_key,
    )
    session.add(execution)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("同 schedule_id+scheduled_at（或同幂等键）的执行已存在，拒绝重复创建") from exc

    schedule = session.get(Schedule, schedule_id) if schedule_id else None
    window = requested_window or (schedule.default_window if schedule else 14)
    if window not in (7, 14, 30):
        window = 14

    warehouses = session.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    if warehouse_ids:
        warehouses = [w for w in warehouses if w.id in warehouse_ids]
    products = session.scalars(select(Product).order_by(Product.id)).all()
    if product_ids:
        products = [p for p in products if p.id in product_ids]

    # 2) 逐 SKU 计算（异常隔离，互不阻断）
    outcomes: list[tuple[Warehouse, Product, dict | None, LineOutcome]] = []
    for warehouse in warehouses:
        for product in products:
            line_dict, outcome = compute_line_for_sku(
                session, warehouse=warehouse, product=product, requested_window=window
            )
            outcomes.append((warehouse, product, line_dict, outcome))

    # 3) 有 valid 明细则按仓库分别创建待审批计划（每行保存点，行级幂等）
    #    计划按仓库维度：同一仓库/SKU 的活动建议部分唯一索引不允许跨仓库合入同一计划。
    valid: list[tuple[Warehouse, Product, dict, LineOutcome]] = [
        (w, p, d, o) for w, p, d, o in outcomes if d is not None
    ]
    plan_ids: list[str] = []
    if valid:
        pairs = [(w.id, p.id) for w, p, _, _ in valid]
        acquire_warehouse_product_locks(session, pairs)
        by_warehouse: dict[str, list[tuple[Warehouse, Product, dict, LineOutcome]]] = {}
        for entry in valid:
            by_warehouse.setdefault(entry[0].id, []).append(entry)

        for warehouse_id, entries in sorted(by_warehouse.items()):
            plan = ReplenishmentPlan(
                warehouse_id=warehouse_id,
                thread_id=None,
                actor_id="system",
                trigger_type=trigger_type,
                status=PLAN_PENDING_APPROVAL,
                requested_window=window,
                planning_date=datetime.now(timezone.utc).date(),
            )
            session.add(plan)
            session.flush()
            plan_ids.append(plan.id)
            created_line_count = 0
            for warehouse, product, line_dict, outcome in entries:
                try:
                    with session.begin_nested():
                        line = PlanLine(plan_id=plan.id, active_for_dedupe=True, **line_dict)
                        session.add(line)
                        session.flush()
                        outcome.line_id = line.id
                        created_line_count += 1
                    close_open_alert(session, warehouse_id=warehouse.id, product_id=product.id, blocker_code="*")
                except IntegrityError:
                    # 行级幂等/活动建议冲突：该行按 blocked 记录，不影响其他 SKU
                    blocked_outcome = LineOutcome(
                        product_id=product.id,
                        flag=LINE_BLOCKED,
                        blocked_code="ACTIVE_REPLENISHMENT_EXISTS",
                        blocked_reason="存在未落实的活动建议，本次扫描跳过",
                    )
                    outcomes = [
                        (w, p, d, o)
                        if not (w.id == warehouse.id and p.id == product.id)
                        else (w, p, None, blocked_outcome)
                        for w, p, d, o in outcomes
                    ]
            if created_line_count == 0:
                # 并发/重复扫描可能让所有候选明细都被活动建议唯一约束拦截。
                # 此时不应遗留无法审批的空计划。
                session.delete(plan)
                session.flush()
                plan_ids.remove(plan.id)

    # 4) 执行明细 + 告警
    counts = {"scanned": len(outcomes), "success": 0, "blocked": 0, "failed": 0}
    for warehouse, product, _line_dict, outcome in outcomes:
        try:
            with session.begin_nested():
                item_status = (
                    EXEC_ITEM_SUCCESS
                    if outcome.flag == LINE_VALID
                    else (EXEC_ITEM_BLOCKED if outcome.flag == LINE_BLOCKED else EXEC_ITEM_FAILED)
                )
                counts[item_status] += 1
                alert_id = None
                if outcome.flag == LINE_BLOCKED:
                    alert = upsert_open_alert(
                        session,
                        alert_type="blocked",
                        warehouse_id=warehouse.id,
                        product_id=product.id,
                        blocker_code=outcome.blocked_code or "unknown",
                        message=outcome.blocked_reason or "",
                        payload={"plan_ids": plan_ids, "trigger_type": trigger_type},
                    )
                    alert_id = alert.id
                session.add(
                    ExecutionItem(
                        execution_id=execution.id,
                        warehouse_id=warehouse.id,
                        product_id=product.id,
                        status=item_status,
                        error_code=outcome.blocked_code,
                        block_reason=outcome.blocked_reason,
                        plan_id=outcome.line_id and _line_plan_id(session, outcome.line_id),
                        plan_line_id=outcome.line_id,
                        alert_id=alert_id,
                    )
                )
        except IntegrityError:
            # 行级幂等（重复执行明细）：跳过
            continue

    # 5) 汇总与审计
    execution.scanned_count = counts["scanned"]
    execution.success_count = counts["success"]
    execution.blocked_count = counts["blocked"]
    execution.failed_count = counts["failed"]
    execution.draft_count = sum(1 for _, _, d, o in outcomes if o.flag == LINE_VALID and o.line_id)
    execution.finished_at = datetime.now(timezone.utc)
    if counts["failed"] == 0:
        execution.status = EXECUTION_SUCCEEDED
    else:
        execution.status = EXECUTION_PARTIAL if counts["success"] > 0 else EXECUTION_FAILED
    execution.error_summary = f"blocked={counts['blocked']}, failed={counts['failed']}" if counts["failed"] else None
    write_audit(
        session,
        actor_id="system",
        action=CMD_SCAN,
        entity_type="execution",
        entity_id=execution.id,
        after_state={"trigger_type": trigger_type, "plan_ids": plan_ids, "counts": counts},
        idempotency_operation_id=None,
    )
    return execution.id


def _line_plan_id(session: Session, line_id: str) -> str | None:
    line = session.get(PlanLine, line_id)
    return line.plan_id if line else None


def run_scan_for_schedule(
    schedule_id: str,
    scheduled_at: datetime | None,
    trigger_type: str,
    triggered_by_actor_id: str | None,
    idempotency_key: str,
) -> str:
    """供 Celery 任务与 admin 立即执行调用（自带提交）。"""
    from app.db import get_session_factory

    factory = get_session_factory()
    with factory() as session:
        execution_id = run_scan(
            session,
            schedule_id=schedule_id,
            scheduled_at=scheduled_at,
            trigger_type=trigger_type,
            triggered_by_actor_id=triggered_by_actor_id,
            idempotency_key=idempotency_key,
        )
        session.commit()
        return execution_id


def rerun_execution(execution_id: str, *, triggered_by_actor_id: str, idempotency_key: str) -> str:
    """手动重跑：复用原执行上下文，创建带来源引用的新执行记录（自带提交）。"""
    from app.db import get_session_factory

    factory = get_session_factory()
    with factory() as session:
        source = session.get(Execution, execution_id)
        if source is None:
            raise ConflictError(f"执行记录不存在 {execution_id}")
        new_id = run_scan(
            session,
            schedule_id=source.schedule_id,
            scheduled_at=None,
            trigger_type=TRIGGER_RERUN,
            triggered_by_actor_id=triggered_by_actor_id,
            idempotency_key=idempotency_key,
            warehouse_ids=None,
            product_ids=None,
            requested_window=None,
            source_execution_id=execution_id,
        )
        session.commit()
        return new_id
