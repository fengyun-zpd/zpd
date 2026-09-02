"""定时扫描集成测试（需求 2.2 / 架构 8）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.constants import (
    EXEC_ITEM_BLOCKED,
    EXEC_ITEM_SUCCESS,
    EXECUTION_RUNNING,
    EXECUTION_SUCCEEDED,
)
from app.errors import ConflictError
from app.models.governance import Execution, ExecutionItem
from app.models.replenishment import ReplenishmentPlan
from app.services.execution_service import run_scan, run_scan_for_schedule


def test_scheduled_scan_creates_plan_and_items(db_session):
    exec_id = run_scan(
        db_session,
        schedule_id=None,
        scheduled_at=datetime.now(timezone.utc),
        trigger_type="manual",
        triggered_by_actor_id="eve",
        idempotency_key=f"exec-{uuid.uuid4().hex}",
    )
    db_session.commit()

    execution = db_session.get(Execution, exec_id)
    assert execution.status in (EXECUTION_RUNNING, EXECUTION_SUCCEEDED)
    assert execution.scanned_count > 0
    assert execution.draft_count >= 1  # SKU-E01 等正常补货
    assert execution.success_count + execution.blocked_count == execution.scanned_count

    items = db_session.query(ExecutionItem).filter(ExecutionItem.execution_id == exec_id).all()
    statuses = {i.product_id: i.status for i in items}
    assert statuses["SKU-E01"] == EXEC_ITEM_SUCCESS
    assert statuses["SKU-E05"] == EXEC_ITEM_BLOCKED  # 无供应商
    assert statuses["SKU-E06"] == EXEC_ITEM_BLOCKED  # 规则冲突

    # 有有效明细 -> 按仓库各创建一张待审批计划（计划按仓库维度）
    plan_ids = {i.plan_id for i in items if i.plan_id}
    assert len(plan_ids) >= 1
    assert all(i.plan_id for i in items if i.status == EXEC_ITEM_SUCCESS)


def test_duplicate_schedule_time_rejected(db_session):
    scheduled_at = datetime.now(timezone.utc)
    key = f"exec-{uuid.uuid4().hex}"
    run_scan(
        db_session,
        schedule_id="sched-1",
        scheduled_at=scheduled_at,
        trigger_type="scheduled",
        triggered_by_actor_id=None,
        idempotency_key=key,
    )
    db_session.commit()
    with pytest.raises(ConflictError):
        run_scan(
            db_session,
            schedule_id="sched-1",
            scheduled_at=scheduled_at,
            trigger_type="scheduled",
            triggered_by_actor_id=None,
            idempotency_key=key,
        )
    db_session.rollback()


def test_no_valid_items_still_recorded(db_session):
    """WH-S 的 E07 等无规则/无供应商 SKU：仍写入执行明细与告警，不创建空计划。"""
    exec_id = run_scan(
        db_session,
        schedule_id=None,
        scheduled_at=datetime.now(timezone.utc),
        trigger_type="manual",
        triggered_by_actor_id="eve",
        idempotency_key=f"exec-{uuid.uuid4().hex}",
        warehouse_ids=["WH-S"],
        product_ids=["SKU-E05", "SKU-E07"],
    )
    db_session.commit()
    execution = db_session.get(Execution, exec_id)
    assert execution.draft_count == 0
    assert execution.blocked_count == 2
    items = db_session.query(ExecutionItem).filter(ExecutionItem.execution_id == exec_id).all()
    assert len(items) == 2
    assert all(i.status == EXEC_ITEM_BLOCKED for i in items)


def test_scan_does_not_leave_empty_plan_when_all_lines_collide(db_session):
    """重复扫描被活动建议拦截时，不遗留没有可审批明细的空计划。"""
    first_id = run_scan(
        db_session,
        schedule_id=None,
        scheduled_at=datetime.now(timezone.utc),
        trigger_type="scheduled",
        triggered_by_actor_id=None,
        idempotency_key=f"exec-{uuid.uuid4().hex}",
        warehouse_ids=["WH-E"],
        product_ids=["SKU-E01"],
    )
    db_session.commit()
    first = db_session.get(Execution, first_id)
    assert first is not None
    assert first.draft_count == 1
    before = {p.id for p in db_session.query(ReplenishmentPlan).all()}

    second_id = run_scan(
        db_session,
        schedule_id=None,
        scheduled_at=datetime.now(timezone.utc),
        trigger_type="scheduled",
        triggered_by_actor_id=None,
        idempotency_key=f"exec-{uuid.uuid4().hex}",
        warehouse_ids=["WH-E"],
        product_ids=["SKU-E01"],
    )
    db_session.commit()
    second = db_session.get(Execution, second_id)
    assert second is not None
    assert second.draft_count == 0
    assert second.blocked_count == 1
    assert {p.id for p in db_session.query(ReplenishmentPlan).all()} == before


def test_run_scan_for_schedule_api_path():
    """admin 立即执行路径：执行主体记为 system，另存 triggered_by_actor_id。"""
    from app.models.governance import Schedule

    factory = None
    from app.db import get_session_factory

    factory = get_session_factory()
    with factory() as session:
        schedule = session.query(Schedule).first()
        schedule_id = schedule.id
    exec_id = run_scan_for_schedule(
        schedule_id=schedule_id,
        scheduled_at=None,
        trigger_type="manual",
        triggered_by_actor_id="eve",
        idempotency_key=f"exec-{uuid.uuid4().hex}",
    )
    with factory() as session:
        execution = session.get(Execution, exec_id)
        assert execution.triggered_by_actor_id == "eve"
        assert execution.schedule_id == schedule_id
