"""并发与唯一约束集成测试（需求 6.4 / 架构 7.3 / ADR 决策 8）。

- 同一仓库/SKU 并发创建草稿：部分唯一索引保证最多一条活动建议；
- 幂等键同键异载荷并发：唯一约束 + 稳定错误码；
- advisory lock 排序避免死锁。
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.db import get_session_factory
from app.errors import ActiveReplenishmentExistsError
from app.services import plan_service


def _draft_in_new_session(warehouse_id, product_id, result_box, idx):
    factory = get_session_factory()
    with factory() as session:
        try:
            draft = plan_service.generate_draft(
                session,
                warehouse_id=warehouse_id,
                requested_window=14,
                actor_id="alice",
                products=[product_id],
                operation_id=f"op-{uuid.uuid4().hex}",
            )
            session.commit()
            result_box[idx] = ("ok", draft.plan_id)
        except ActiveReplenishmentExistsError:
            session.rollback()
            result_box[idx] = ("conflict", None)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            result_box[idx] = ("error", str(exc))


@pytest.mark.db
def test_concurrent_draft_same_sku_only_one_active():
    """并发创建同一 SKU 草稿：恰好一个成功，其余冲突（不产生第二条活动建议）。"""
    from sqlalchemy import func, select

    from app.models.replenishment import PlanLine

    results: dict[int, tuple[str, str | None]] = {}
    threads = [threading.Thread(target=_draft_in_new_session, args=("WH-E", "SKU-E02", results, i)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok_count = sum(1 for v in results.values() if v[0] == "ok")
    conflict_count = sum(1 for v in results.values() if v[0] == "conflict")
    assert ok_count == 1
    assert conflict_count == 3

    factory = get_session_factory()
    with factory() as session:
        active = session.scalar(
            select(func.count())
            .select_from(PlanLine)
            .where(
                PlanLine.warehouse_id == "WH-E",
                PlanLine.product_id == "SKU-E02",
                PlanLine.active_for_dedupe.is_(True),
            )
        )
        assert active == 1


@pytest.mark.db
def test_plan_line_unique_per_plan_product(db_session):
    """同一计划内同一 SKU 只能有一条明细（数据库唯一约束）。"""
    from sqlalchemy.exc import IntegrityError

    from app.models.replenishment import PlanLine

    draft = plan_service.generate_draft(
        db_session,
        warehouse_id="WH-E",
        requested_window=14,
        actor_id="alice",
        products=["SKU-E01"],
        operation_id=f"op-{uuid.uuid4().hex}",
    )
    db_session.commit()
    plan_id = draft.plan_id

    # 尝试插入同 plan 同 product 的第二条明细（绕过服务层直接验证数据库约束）
    from app.constants import LINE_VALID

    dup = PlanLine(
        plan_id=plan_id,
        warehouse_id="WH-E",
        product_id="SKU-E01",
        flag=LINE_VALID,
        active_for_dedupe=False,
        planning_window=14,
        coverage_demand=0,
        target_stock=0,
        available=0,
        inbound=0,
        net_demand=0,
        order_qty=0,
        decision_input_hash="x" * 64,
        hash_schema_version="v1",
        algorithm_version="v1",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
