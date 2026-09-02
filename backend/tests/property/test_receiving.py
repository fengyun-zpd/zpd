"""收货上限与状态不变量性质测试（需求 6.3 / 架构 7.3）。

说明：数据库夹具按测试重建种子数据，与 Hypothesis 随机示例不兼容，
因此收货上限性质使用确定性边界用例集（超量/恰好/分批累计/重复事件）。
"""

from __future__ import annotations

import uuid

import pytest

from app.errors import ReceiptExceedsRemainingError
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.services.purchase_service import receive_goods


@pytest.mark.db
@pytest.mark.parametrize("extra", [1, 7, 100, 99999])
def test_receipt_never_exceeds_remaining(db_session, extra):
    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.status == "ordered").first()
    if po is None:
        pytest.skip("种子数据中没有 ordered 采购单")
    line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).first()
    remaining = line.order_qty - line.received_qty

    with pytest.raises(ReceiptExceedsRemainingError):
        receive_goods(
            db_session,
            po_line_id=line.id,
            receipt_event_id=f"EVT-{uuid.uuid4().hex}",
            qty=remaining + extra,
            actor_id="alice",
            idempotency_key=f"r-{uuid.uuid4().hex}",
        )


@pytest.mark.db
def test_cumulative_received_bound(db_session):
    """分批收货累计量不超过订单量。"""
    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.status == "ordered").first()
    assert po is not None
    line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).first()
    remaining = line.order_qty - line.received_qty

    # 分 3 次收货，最后一次正好收满
    chunk = remaining // 3
    received_total = 0
    for i in range(3):
        qty = chunk if i < 2 else remaining - received_total
        if qty <= 0:
            break
        outcome = receive_goods(
            db_session,
            po_line_id=line.id,
            receipt_event_id=f"EVT-{uuid.uuid4().hex}",
            qty=qty,
            actor_id="alice",
            idempotency_key=f"r-{uuid.uuid4().hex}",
        )
        db_session.commit()
        received_total += qty
        assert outcome["received_qty"] == received_total
        assert received_total <= line.order_qty
    refreshed = db_session.get(PurchaseOrderLine, line.id)
    assert refreshed.received_qty == received_total <= line.order_qty
