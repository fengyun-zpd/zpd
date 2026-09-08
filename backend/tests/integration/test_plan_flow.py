"""集成测试：补货计划完整闭环（需求 5 / 6 / 架构 7）。

覆盖：草稿→审批→建单→下单→收货→关闭；PLAN_STALE；活动建议防重；
空计划拦截；整单驳回后防重释放；修订替代；下单未知状态恢复；取消守卫。
"""

from __future__ import annotations

import uuid

import pytest

from app.constants import (
    CMD_PLACE_ORDER,
    PLAN_APPROVED,
    PLAN_PENDING_APPROVAL,
    PLAN_REJECTED,
    PLAN_SUPERSEDED,
    PO_CANCELLED,
    PO_CLOSED,
    PO_CREATED,
    PO_ORDER_UNKNOWN,
    PO_ORDERED,
)
from app.errors import (
    ActiveReplenishmentExistsError,
    InvalidStateTransitionError,
    PlanStaleError,
)
from app.models.governance import AuditLog
from app.models.inventory import Quant
from app.models.purchasing import PurchaseOrder, PurchaseOrderAttempt, PurchaseOrderLine
from app.models.replenishment import PlanLine, ReplenishmentPlan
from app.services import plan_service, purchase_service
from app.services.purchase_service import SupplierQueryResult, SupplierResult


class FakeAdapter:
    """进程内模拟供应商（用于集成测试，行为可控）。"""

    def __init__(self, outcome: str = "success", query_found: bool = True) -> None:
        self.outcome = outcome
        self.query_found = query_found
        self.created: dict[str, str] = {}

    def place_order(self, supplier_key: str, _payload: dict) -> SupplierResult:
        if self.outcome == "explicit_failure":
            return SupplierResult(outcome="explicit_failure", raw="rejected")
        if self.outcome == "timeout":
            # 模拟超时但实际已创建：查询可确认
            self.created[supplier_key] = "EXT-TIMEOUT"
            return SupplierResult(outcome="timeout", raw="timeout")
        ext = f"EXT-{uuid.uuid4().hex[:8]}"
        self.created[supplier_key] = ext
        return SupplierResult(outcome="success", external_order_no=ext)

    def query_order(self, supplier_key: str) -> SupplierQueryResult:
        if self.query_found:
            return SupplierQueryResult(found=True, external_order_no=self.created.get(supplier_key, "EXT-Q"))
        return SupplierQueryResult(found=False)


@pytest.fixture()
def adapter():
    return FakeAdapter()


def _draft_e01(session, *, window=14, actor="alice"):
    return plan_service.generate_draft(
        session,
        warehouse_id="WH-E",
        requested_window=window,
        actor_id=actor,
        products=["SKU-E01"],
        operation_id=f"op-{uuid.uuid4().hex}",
    )


def test_full_closed_loop(db_session, adapter):
    # 1) 草稿
    draft = _draft_e01(db_session)
    db_session.commit()
    assert draft.created is True and draft.plan_id
    plan = db_session.get(ReplenishmentPlan, draft.plan_id)
    assert plan.status == PLAN_PENDING_APPROVAL
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    assert line.flag == "valid"
    assert line.active_for_dedupe is True
    assert line.order_qty > 0
    assert line.supplier_id == "SUP-001"
    assert len(line.decision_input_hash) == 64

    # 2) 审批
    decided = plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert decided.status == PLAN_APPROVED

    # 3) 建单
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert len(pos) == 1
    po = pos[0]
    assert po.status == PO_CREATED
    # 建单后活动状态解除
    db_session.refresh(line)
    assert line.active_for_dedupe is False

    # 4) 下单（成功）
    outcome = purchase_service.place_order(
        db_session,
        po_id=po.id,
        actor_id="eve",
        idempotency_key=f"place-{uuid.uuid4().hex}",
        adapter=adapter,
    )
    db_session.expire_all()  # 写回使用新会话，刷新本会话视图
    assert outcome["status"] == "ordered"
    assert outcome["external_order_no"].startswith("EXT-")
    assert db_session.get(PurchaseOrder, po.id).status == PO_ORDERED
    attempt = db_session.query(PurchaseOrderAttempt).filter(PurchaseOrderAttempt.purchase_order_id == po.id).first()
    assert attempt.status == "succeeded"

    # 5) 收货（分批）
    po_line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).first()
    qty = po_line.order_qty
    r1 = purchase_service.receive_goods(
        db_session,
        po_line_id=po_line.id,
        receipt_event_id=f"EVT-{uuid.uuid4().hex}",
        qty=qty // 2,
        actor_id="eve",
        idempotency_key=f"rcv-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert r1["po_status"] == "partially_received"
    r2 = purchase_service.receive_goods(
        db_session,
        po_line_id=po_line.id,
        receipt_event_id=f"EVT-{uuid.uuid4().hex}",
        qty=qty - qty // 2,
        actor_id="eve",
        idempotency_key=f"rcv-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert r2["po_status"] == "received"

    # 6) 关闭
    closed = purchase_service.close_purchase_order(
        db_session,
        po_id=po.id,
        actor_id="eve",
        idempotency_key=f"close-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert closed["status"] == PO_CLOSED

    # 审计记录
    audits = db_session.query(AuditLog).filter(AuditLog.entity_id == po.id).all()
    assert len(audits) >= 4
    assert any(a.action == CMD_PLACE_ORDER and a.actor_id == "eve" for a in audits)


def test_plan_stale_on_approval(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()

    # 审批前库存变化 -> PLAN_STALE
    quant = db_session.query(Quant).filter(Quant.warehouse_id == "WH-E", Quant.product_id == "SKU-E01").first()
    quant.on_hand += 100
    db_session.commit()

    with pytest.raises(PlanStaleError):
        plan_service.decide_plan(
            db_session,
            plan_id=draft.plan_id,
            actor_id="carol",
            mode="approve",
            decisions={line.id: "approve"},
            idempotency_key=f"app-{uuid.uuid4().hex}",
        )
    db_session.rollback()


def test_plan_stale_on_create_po(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()

    quant = db_session.query(Quant).filter(Quant.warehouse_id == "WH-E", Quant.product_id == "SKU-E01").first()
    quant.on_hand += 50
    db_session.commit()

    with pytest.raises(PlanStaleError):
        plan_service.create_purchase_orders(
            db_session,
            plan_id=draft.plan_id,
            actor_id="dave",
            idempotency_key=f"po-{uuid.uuid4().hex}",
        )
    db_session.rollback()


def test_active_replenishment_dedupe(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    assert draft.created is True
    with pytest.raises(ActiveReplenishmentExistsError) as exc_info:
        _draft_e01(db_session)
    detail = exc_info.value.detail
    assert detail["plan_id"] == draft.plan_id
    assert detail["plan_status"] == PLAN_PENDING_APPROVAL
    assert detail["order_qty"] > 0
    db_session.rollback()


def test_no_valid_lines_no_empty_plan(db_session):
    """E05 无供应商 / E06 规则冲突 / E07 华南仓无规则 -> 不创建空计划。"""
    draft = plan_service.generate_draft(
        db_session,
        warehouse_id="WH-S",
        requested_window=14,
        actor_id="alice",
        products=["SKU-E05", "SKU-E06", "SKU-E07"],
        operation_id=f"op-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert draft.created is False
    assert draft.plan_id is None
    flags = {o.product_id: o.flag for o in draft.lines}
    assert flags["SKU-E05"] == "blocked"
    assert flags["SKU-E06"] == "blocked"
    assert flags["SKU-E07"] == "blocked"


def test_approve_all_excluded_rejects_and_releases(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()

    plan = plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={},
        idempotency_key=f"app-{uuid.uuid4().hex}",  # 未勾选 -> 全部排除
    )
    db_session.commit()
    assert plan.status == PLAN_REJECTED
    db_session.refresh(line)
    assert line.active_for_dedupe is False

    # 防重释放后可重新生成草稿
    draft2 = _draft_e01(db_session)
    db_session.commit()
    assert draft2.created is True


def test_revision_supersedes(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()

    new_plan = plan_service.supersede_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="alice",
        operation_id=f"rev-{uuid.uuid4().hex}",
    )
    db_session.commit()
    old = db_session.get(ReplenishmentPlan, draft.plan_id)
    assert old.status == PLAN_SUPERSEDED
    assert new_plan.revision_of_plan_id == draft.plan_id


def test_revision_forbidden_after_po(db_session, adapter):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()
    plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()

    with pytest.raises(InvalidStateTransitionError):
        plan_service.supersede_plan(
            db_session,
            plan_id=draft.plan_id,
            actor_id="alice",
            operation_id=f"rev-{uuid.uuid4().hex}",
        )
    db_session.rollback()


def test_place_order_explicit_failure_returns_created(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()

    failing = FakeAdapter(outcome="explicit_failure")
    from app.errors import StockMindError

    # 明确失败：命令完成、回到 po_created、attempt 记录 failed（不抛异常）
    fail_key = f"place-{uuid.uuid4().hex}"
    outcome = purchase_service.place_order(
        db_session,
        po_id=pos[0].id,
        actor_id="dave",
        idempotency_key=fail_key,
        adapter=failing,
    )
    db_session.commit()
    db_session.expire_all()
    assert outcome["status"] == PO_CREATED
    assert outcome["attempt_status"] == "failed"
    assert db_session.get(PurchaseOrder, pos[0].id).status == PO_CREATED

    # 同键重放：返回已记录的失败（不允许重复执行）
    with pytest.raises(StockMindError):
        purchase_service.place_order(
            db_session,
            po_id=pos[0].id,
            actor_id="dave",
            idempotency_key=fail_key,
            adapter=failing,
        )
    db_session.rollback()

    # 明确失败后可发起新尝试（新幂等键）
    ok = purchase_service.place_order(
        db_session,
        po_id=pos[0].id,
        actor_id="dave",
        idempotency_key=f"place2-{uuid.uuid4().hex}",
        adapter=FakeAdapter(),
    )
    db_session.expire_all()
    assert ok["status"] == "ordered"
    attempts = db_session.query(PurchaseOrderAttempt).filter(PurchaseOrderAttempt.purchase_order_id == pos[0].id).all()
    assert len(attempts) == 2


def test_order_unknown_then_query_recovery(db_session):
    """超时进入 order_unknown；查询确认已创建 -> ordered。"""
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()

    timeout_adapter = FakeAdapter(outcome="timeout")
    outcome = purchase_service.place_order(
        db_session,
        po_id=pos[0].id,
        actor_id="dave",
        idempotency_key=f"place-{uuid.uuid4().hex}",
        adapter=timeout_adapter,
    )
    db_session.expire_all()  # place_order 写回使用新会话，刷新视图
    assert outcome["status"] == PO_ORDER_UNKNOWN

    recovered = purchase_service.query_unknown_order(
        db_session,
        po_id=pos[0].id,
        actor_id="eve",
        idempotency_key=f"query-{uuid.uuid4().hex}",
        adapter=timeout_adapter,
    )
    db_session.commit()
    assert recovered["status"] == PO_ORDERED
    assert recovered["external_order_no"] == "EXT-TIMEOUT"


def test_order_unknown_query_not_found_returns_created(db_session):
    """查询确认外部未创建 -> 回到 po_created；可换新尝试。"""
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()

    unknown = FakeAdapter(outcome="timeout")
    purchase_service.place_order(
        db_session,
        po_id=pos[0].id,
        actor_id="dave",
        idempotency_key=f"place-{uuid.uuid4().hex}",
        adapter=unknown,
    )
    db_session.expire_all()
    not_found = FakeAdapter(outcome="timeout", query_found=False)
    recovered = purchase_service.query_unknown_order(
        db_session,
        po_id=pos[0].id,
        actor_id="dave",
        idempotency_key=f"query-{uuid.uuid4().hex}",
        adapter=not_found,
    )
    db_session.commit()
    assert recovered["status"] == PO_CREATED


def test_cancel_guards(db_session):
    draft = _draft_e01(db_session)
    db_session.commit()
    line = db_session.query(PlanLine).filter(PlanLine.plan_id == draft.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key=f"app-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft.plan_id,
        actor_id="dave",
        idempotency_key=f"po-{uuid.uuid4().hex}",
    )
    db_session.commit()

    # po_created 无外部尝试 -> 可取消
    cancelled = purchase_service.cancel_purchase_order(
        db_session,
        po_id=pos[0].id,
        actor_id="eve",
        idempotency_key=f"cancel-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert cancelled["status"] == PO_CANCELLED

    # 已下单不能取消
    draft2 = _draft_e01(db_session)
    db_session.commit()
    line2 = db_session.query(PlanLine).filter(PlanLine.plan_id == draft2.plan_id).first()
    plan_service.decide_plan(
        db_session,
        plan_id=draft2.plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line2.id: "approve"},
        idempotency_key=f"app2-{uuid.uuid4().hex}",
    )
    db_session.commit()
    pos2 = plan_service.create_purchase_orders(
        db_session,
        plan_id=draft2.plan_id,
        actor_id="dave",
        idempotency_key=f"po2-{uuid.uuid4().hex}",
    )
    db_session.commit()
    purchase_service.place_order(
        db_session,
        po_id=pos2[0].id,
        actor_id="dave",
        idempotency_key=f"place2-{uuid.uuid4().hex}",
        adapter=FakeAdapter(),
    )
    with pytest.raises(InvalidStateTransitionError):
        purchase_service.cancel_purchase_order(
            db_session,
            po_id=pos2[0].id,
            actor_id="dave",
            idempotency_key=f"cancel2-{uuid.uuid4().hex}",
        )
    db_session.rollback()
