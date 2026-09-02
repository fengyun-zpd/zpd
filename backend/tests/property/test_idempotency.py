"""幂等性质与数据库测试（需求 6.4 / 架构 7.3）。

说明：数据库夹具按测试重建种子数据，Hypothesis 随机示例与逐示例重建不兼容，
因此数据库幂等性质使用确定性边界用例集；纯逻辑性质（取整等）由
property/test_rounding.py 的 Hypothesis 覆盖。
"""

from __future__ import annotations

import uuid

import pytest

from app.constants import CMD_RECEIVE
from app.errors import IdempotencyKeyReusedError, OperationInProgressError
from app.models.purchasing import ReceiptEvent
from app.services.idempotency import IdempotencyGuard


@pytest.mark.db
@pytest.mark.parametrize(
    "payload",
    [
        {"qty": 1},
        {"qty": 999, "line": "x"},
        {},
        {"nested": {"a": [1, 2], "b": None}},
    ],
)
def test_idempotency_same_key_same_payload_replays(db_session, payload):
    key = f"k-{uuid.uuid4().hex}"
    first = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload=payload,
    )
    assert first.begin().action == "proceed"
    first.succeed({"ok": True}, entity_type="po_line", entity_id="line-1")
    db_session.commit()

    second = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload=payload,
    )
    result = second.begin()
    assert result.action == "replay_success"
    assert result.response == {"ok": True}


@pytest.mark.db
def test_idempotency_same_key_different_payload_conflict(db_session):
    key = f"k-{uuid.uuid4().hex}"
    guard = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    assert guard.begin().action == "proceed"
    guard.succeed({"ok": True})
    db_session.commit()

    guard2 = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 20},
    )
    with pytest.raises(IdempotencyKeyReusedError):
        guard2.begin()


@pytest.mark.db
def test_idempotency_in_progress_returns_202(db_session):
    key = f"k-{uuid.uuid4().hex}"
    guard = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    assert guard.begin().action == "proceed"
    db_session.commit()  # in_progress 已提交

    guard2 = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    with pytest.raises(OperationInProgressError):
        guard2.begin()


@pytest.mark.db
def test_retryable_recovers_on_same_operation(db_session):
    key = f"k-{uuid.uuid4().hex}"
    guard = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    assert guard.begin().action == "proceed"
    guard.mark_retryable()
    db_session.commit()

    guard2 = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    assert guard2.begin().action == "proceed"  # retryable 可在同一记录上恢复


@pytest.mark.db
def test_unknown_not_replayable(db_session):
    key = f"k-{uuid.uuid4().hex}"
    guard = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    assert guard.begin().action == "proceed"
    guard.mark_unknown()
    db_session.commit()

    from app.errors import ExternalUnknownError

    guard2 = IdempotencyGuard(
        db_session,
        principal_id="alice",
        command_type=CMD_RECEIVE,
        aggregate_ref="line-1",
        idempotency_key=key,
        payload={"qty": 10},
    )
    with pytest.raises(ExternalUnknownError):
        guard2.begin()


@pytest.mark.db
def test_receipt_event_reuse_rejected(db_session):
    """相同 receipt_event_id 异载荷复用必须拒绝；同载荷返回原结果。"""
    from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
    from app.services.purchase_service import receive_goods

    po = db_session.query(PurchaseOrder).filter(PurchaseOrder.status == "ordered").first()
    assert po is not None
    line = db_session.query(PurchaseOrderLine).filter(PurchaseOrderLine.purchase_order_id == po.id).first()

    first = receive_goods(
        db_session,
        po_line_id=line.id,
        receipt_event_id="EVT-001",
        qty=10,
        actor_id="alice",
        idempotency_key=f"r-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert first["po_status"] in ("partially_received", "received")

    # 同载荷重复 -> 原结果
    replay = receive_goods(
        db_session,
        po_line_id=line.id,
        receipt_event_id="EVT-001",
        qty=10,
        actor_id="alice",
        idempotency_key=f"r-{uuid.uuid4().hex}",
    )
    db_session.commit()
    assert replay["status"] == "duplicate_replay"

    # 异载荷复用 -> 拒绝
    from app.errors import ReceiptEventReusedError

    with pytest.raises(ReceiptEventReusedError):
        receive_goods(
            db_session,
            po_line_id=line.id,
            receipt_event_id="EVT-001",
            qty=20,
            actor_id="alice",
            idempotency_key=f"r-{uuid.uuid4().hex}",
        )

    # 库存只增加一次
    events = db_session.query(ReceiptEvent).filter(ReceiptEvent.receipt_event_id == "EVT-001").all()
    assert len(events) == 1
