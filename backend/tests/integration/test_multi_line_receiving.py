"""验收回归：多行采购单分批收货（需求 6.3 / 说明书 §4.5）。

Bug #1 根因：receive_goods 中"单行收满即把整张 PO 置为 received"，
导致多行 PO 其余明细无法再收货、未收齐即可关闭。
Bug #2 根因：create_purchase_orders 未过滤 order_qty=0 的明细（净需求 0 也建 0 数量行）。

期望：
- 多行 PO 收满一行后状态仍为 partially_received；
- 全部"有数量"行收齐后才 received；
- 建单时 order_qty=0 的明细不进入采购单。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.constants import PLAN_APPROVED, PO_CLOSED, PO_ORDERED, PO_PARTIALLY_RECEIVED, PO_RECEIVED
from app.errors import InvalidStateTransitionError, ValidationError
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.models.replenishment import PlanLine, ReplenishmentPlan
from app.services import plan_service, purchase_service
from app.services.purchase_service import SupplierQueryResult, SupplierResult


class FakeAdapter:
    """进程内模拟供应商（用于集成测试，行为可控）。"""

    def __init__(self, outcome: str = "success", query_found: bool = True) -> None:
        self.outcome = outcome
        self.query_found = query_found
        self.created: dict[str, str] = {}

    def place_order(self, key: str, _payload: dict) -> SupplierResult:
        if self.outcome == "explicit_failure":
            return SupplierResult("explicit_failure", raw="rejected")
        ext = f"EXT-{uuid.uuid4().hex[:6]}"
        self.created[key] = ext
        return SupplierResult("success", ext)

    def query_order(self, key: str) -> SupplierQueryResult:
        return SupplierQueryResult(self.query_found, self.created.get(key, "EXT-Q"))


def _make_2line_po(db_session) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    """通过真实计划流程构造一张含 2 行有数量明细的采购单。"""
    draft = plan_service.generate_draft(
        db_session,
        warehouse_id="WH-E",
        requested_window=14,
        actor_id="alice",
        products=["SKU-E07", "SKU-E14"],
        operation_id=f"draft-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert draft.created and draft.plan_id
    draft_lines = db_session.scalars(
        select(PlanLine).where(PlanLine.plan_id == draft.plan_id, PlanLine.flag == "valid")
    ).all()
    assert len(draft_lines) == 2
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve" for line in draft_lines},
        idempotency_key=f"approve-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert len(pos) == 1
    po = pos[0]
    lines = db_session.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)).all()
    assert len(lines) == 2
    db_session.commit()
    return po, lines


def test_multi_line_po_stays_partially_received_until_all_lines_done(db_session):
    """Bug #1：收满一行后 PO 仍为 partially_received；全部行收齐才 received。"""
    po, lines = _make_2line_po(db_session)

    purchase_service.place_order(
        db_session,
        po_id=po.id,
        actor_id="dave",
        idempotency_key=f"pl-{uuid.uuid4().hex}",
        adapter=FakeAdapter(),
    )
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(PurchaseOrder, po.id).status == PO_ORDERED

    # 收满第一行全量
    first = db_session.get(PurchaseOrderLine, lines[0].id)
    out1 = purchase_service.receive_goods(
        db_session,
        po_line_id=first.id,
        receipt_event_id=f"EVT-{uuid.uuid4().hex}",
        qty=first.order_qty,
        actor_id="bob",
        idempotency_key=f"rcv-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert out1["po_status"] == PO_PARTIALLY_RECEIVED, (
        f"收满一行后 PO 应为 partially_received，实际 {out1['po_status']}"
    )

    # 第二行仍可收货
    db_session.expire_all()
    second = db_session.scalars(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.purchase_order_id == po.id,
            PurchaseOrderLine.id != first.id,
        )
    ).all()[0]
    out2 = purchase_service.receive_goods(
        db_session,
        po_line_id=second.id,
        receipt_event_id=f"EVT-{uuid.uuid4().hex}",
        qty=second.order_qty,
        actor_id="bob",
        idempotency_key=f"rcv-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert out2["po_status"] == PO_RECEIVED, f"全部行收齐后 PO 应为 received，实际 {out2['po_status']}"

    # 关闭成功
    closed = purchase_service.close_purchase_order(
        db_session,
        po_id=po.id,
        actor_id="dave",
        idempotency_key=f"cl-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert closed["status"] == PO_CLOSED


def test_zero_qty_line_does_not_block_po_received(db_session):
    """Bug #2 缓解：历史 0 数量行不阻止其他行收齐后 PO 进入 received。"""
    po, lines = _make_2line_po(db_session)
    # 用一条合法计划明细追加历史 0 数量采购行，保持 plan_line_id 非空契约。
    zero_draft = plan_service.generate_draft(
        db_session,
        warehouse_id="WH-E",
        requested_window=14,
        actor_id="alice",
        products=["SKU-E07"],
        operation_id=f"draft-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert zero_draft.created and zero_draft.plan_id
    zero_plan_line = db_session.scalars(
        select(PlanLine).where(PlanLine.plan_id == zero_draft.plan_id, PlanLine.flag == "valid")
    ).one()
    plan_service.decide_plan(
        db_session,
        plan_id=zero_draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={zero_plan_line.id: "approve"},
        idempotency_key=f"approve-{uuid.uuid4().hex}",
    )
    db_session.commit()
    zero_plan_line.active_for_dedupe = False
    zero = PurchaseOrderLine(
        purchase_order_id=po.id,
        plan_line_id=zero_plan_line.id,
        product_id="SKU-E07",
        order_qty=0,
        received_qty=0,
        unit_price=1,
    )
    db_session.add(zero)
    db_session.commit()

    purchase_service.place_order(
        db_session,
        po_id=po.id,
        actor_id="dave",
        idempotency_key=f"pl-{uuid.uuid4().hex}",
        adapter=FakeAdapter(),
    )
    db_session.commit()
    db_session.expire_all()

    for line in lines:
        db_session.expire_all()
        cur = db_session.get(PurchaseOrderLine, line.id)
        purchase_service.receive_goods(
            db_session,
            po_line_id=cur.id,
            receipt_event_id=f"EVT-{uuid.uuid4().hex}",
            qty=cur.order_qty,
            actor_id="bob",
            idempotency_key=f"rcv-{uuid.uuid4().hex}",
        )
        db_session.commit()
    db_session.expire_all()
    final_po = db_session.get(PurchaseOrder, po.id)
    assert final_po.status == PO_RECEIVED, f"有数量行全收齐后（含 0 数量行）PO 应为 received，实际 {final_po.status}"


def test_create_purchase_orders_rejects_all_zero_qty_plan(db_session):
    """Bug #2 收口：全零计划（所有 valid 明细 order_qty=0）建单必须返回稳定校验错误，不能静默成功。

    “订货量为 0 时不建单”的业务规则不变：不创建任何采购单；接口改为可理解的校验错误，
    不再返回空数组让调用方误以为建单成功。错误发生在任何副作用之前（计划版本不增、无审计）。
    """
    # 构造一张已批准计划：1 行 valid，order_qty=0
    draft = plan_service.generate_draft(
        db_session,
        warehouse_id="WH-E",
        requested_window=14,
        actor_id="alice",
        products=["SKU-E01"],
        operation_id=f"op-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert draft.created
    lines = db_session.scalars(
        select(PlanLine).where(PlanLine.plan_id == draft.plan_id, PlanLine.flag == "valid")
    ).all()
    if not lines:
        pytest.skip("种子无 valid 明细")
    line = lines[0]
    # 直接改该行 order_qty=0（模拟净需求为 0 的批准明细）
    line.order_qty = 0
    db_session.commit()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"a-{uuid.uuid4().hex}",
    )
    db_session.commit()
    plan = db_session.get(ReplenishmentPlan, draft.plan_id)
    version_before = plan.version

    with pytest.raises(ValidationError, match="无需建单"):
        plan_service.create_purchase_orders(
            db_session,
            plan_id=draft.plan_id,
            actor_id="dave",
            idempotency_key=f"po-{uuid.uuid4().hex}",
        )
    db_session.rollback()
    # 不应创建任何采购单；计划状态/版本不被破坏（无副作用）
    db_session.expire_all()
    assert db_session.scalar(select(PlanLine).where(PlanLine.plan_id == draft.plan_id).limit(1)) is not None
    assert db_session.scalar(select(PurchaseOrder).where(PurchaseOrder.plan_id == draft.plan_id)) is None
    plan2 = db_session.get(ReplenishmentPlan, draft.plan_id)
    assert plan2.status == PLAN_APPROVED
    assert plan2.version == version_before


def test_create_purchase_orders_rejects_duplicate_po_for_same_plan(db_session):
    """同一已批准计划重复建单必须被拒绝（稳定错误），不得走到 DB 唯一约束的 500。"""
    draft = plan_service.generate_draft(
        db_session,
        warehouse_id="WH-E",
        requested_window=14,
        actor_id="alice",
        products=["SKU-E01"],
        operation_id=f"op-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert draft.created
    line = db_session.scalars(
        select(PlanLine).where(PlanLine.plan_id == draft.plan_id, PlanLine.flag == "valid")
    ).first()
    if line is None:
        pytest.skip("种子无 valid 明细")
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"a-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert len(pos) == 1

    with pytest.raises(InvalidStateTransitionError, match="已创建过采购单"):
        plan_service.create_purchase_orders(
            db_session,
            plan_id=draft.plan_id,
            actor_id="dave",
            idempotency_key=f"po2-{uuid.uuid4().hex}",
        )
    db_session.rollback()
    # 只存在一张采购单
    db_session.expire_all()
    po_count_stmt = select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.plan_id == draft.plan_id)
    count = db_session.scalar(po_count_stmt)
    assert count == 1
