"""采购域服务（需求 6.2 / 6.3 / 6.4、架构 7.2 / 7.3、ADR 决策 8、宪法第十四条）。

下单协议固定为：先在数据库事务中持久化 attempt 与 ordering 并提交，再调用供应商，
最后按明确成功/明确失败/歧义结果写回。超时/断连/响应歧义 -> order_unknown，
只能查询外部事实恢复，禁止盲目重试或换键重下。收货带唯一 receipt_event_id 防重。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import (
    ATTEMPT_ATTEMPTING,
    ATTEMPT_FAILED,
    ATTEMPT_QUERIED_CREATED,
    ATTEMPT_QUERIED_NOT_FOUND,
    ATTEMPT_SUCCEEDED,
    ATTEMPT_UNKNOWN,
    CMD_CANCEL_PO,
    CMD_CLOSE_PO,
    CMD_PLACE_ORDER,
    CMD_QUERY_ORDER,
    CMD_RECEIVE,
    HASH_SCHEMA_VERSION,
    PO_CANCELLED,
    PO_CLOSED,
    PO_CREATED,
    PO_ORDER_UNKNOWN,
    PO_ORDERED,
    PO_ORDERING,
    PO_PARTIALLY_RECEIVED,
    PO_RECEIVED,
)
from app.errors import (
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    ReceiptEventReusedError,
    ReceiptExceedsRemainingError,
    ValidationError,
)
from app.models.inventory import Quant
from app.models.purchasing import (
    PurchaseOrder,
    PurchaseOrderAttempt,
    PurchaseOrderLine,
    ReceiptEvent,
)
from app.services.audit import write_audit
from app.services.hashing import payload_hash
from app.services.idempotency import IdempotencyGuard, replay_failure
from app.services.locks import acquire_warehouse_product_locks

# ---------------------------------------------------------------- 供应商适配器协议


@dataclass
class SupplierResult:
    outcome: str  # "success" | "explicit_failure" | "timeout" | "unknown"
    external_order_no: str | None = None
    raw: str = ""


@dataclass
class SupplierQueryResult:
    found: bool
    external_order_no: str | None = None


class SupplierAdapter(Protocol):
    def place_order(self, supplier_key: str, payload: dict) -> SupplierResult: ...
    def query_order(self, supplier_key: str) -> SupplierQueryResult: ...


# ---------------------------------------------------------------- 状态迁移守卫


def _po_transition(po: PurchaseOrder, target: str) -> None:
    from app.constants import PO_ALLOWED_TRANSITIONS

    if target not in PO_ALLOWED_TRANSITIONS.get(po.status, ()):
        raise InvalidStateTransitionError(f"非法采购单状态迁移 {po.status} -> {target}")


# ---------------------------------------------------------------- 下单


def _create_attempt_and_ordering(session: Session, po: PurchaseOrder, internal_key: str) -> PurchaseOrderAttempt:
    """事务内：创建不可变 attempt 并把采购单改为 ordering（提交后外呼）。"""
    _po_transition(po, PO_ORDERING)
    max_no = session.scalar(
        select(func.coalesce(func.max(PurchaseOrderAttempt.attempt_no), 0)).where(
            PurchaseOrderAttempt.purchase_order_id == po.id
        )
    )
    assert max_no is not None
    attempt_no = int(max_no) + 1
    supplier_key = f"po:{po.id}:att:{attempt_no}"
    request_payload = {
        "purchase_order_id": po.id,
        "supplier_id": po.supplier_id,
        "warehouse_id": po.warehouse_id,
        "attempt_no": attempt_no,
    }
    attempt = PurchaseOrderAttempt(
        purchase_order_id=po.id,
        attempt_no=attempt_no,
        internal_idempotency_key=internal_key,
        supplier_idempotency_key=supplier_key,
        request_hash=payload_hash(request_payload),
        hash_schema_version=HASH_SCHEMA_VERSION,
        status=ATTEMPT_ATTEMPTING,
    )
    session.add(attempt)
    po.status = PO_ORDERING
    po.version += 1
    session.flush()
    return attempt


def _write_back(
    session: Session,
    *,
    po: PurchaseOrder,
    attempt: PurchaseOrderAttempt,
    guard: IdempotencyGuard,
    result: SupplierResult,
    request_id: str | None = None,
) -> dict:
    """外呼完成后写回：明确成功/明确失败/歧义按允许转换表落库。"""
    current = session.get(PurchaseOrder, po.id, with_for_update=True)
    attempt_row = session.get(PurchaseOrderAttempt, attempt.id, with_for_update=True)
    assert current is not None and attempt_row is not None  # 下单事务中已创建
    before = {"status": current.status, "version": current.version}

    if result.outcome == "success":
        if current.status in (PO_ORDERING, PO_ORDER_UNKNOWN):
            _po_transition(current, PO_ORDERED)
            current.status = PO_ORDERED
            current.version += 1
        attempt_row.status = ATTEMPT_SUCCEEDED
        attempt_row.external_order_no = result.external_order_no
        attempt_row.response_summary = result.raw
        guard.succeed(
            {
                "status": "ordered",
                "external_order_no": result.external_order_no,
                "attempt_no": attempt_row.attempt_no,
            },
            entity_type="purchase_order",
            entity_id=current.id,
        )
    elif result.outcome == "explicit_failure":
        attempt_row.status = ATTEMPT_FAILED
        attempt_row.response_summary = result.raw
        if current.status == PO_ORDERING:
            _po_transition(current, PO_CREATED)
            current.status = PO_CREATED
            current.version += 1
        guard.fail(_explicit_failure_error())
    else:  # timeout / unknown
        attempt_row.status = ATTEMPT_UNKNOWN
        attempt_row.response_summary = result.raw
        if current.status == PO_ORDERING:
            _po_transition(current, PO_ORDER_UNKNOWN)
            current.status = PO_ORDER_UNKNOWN
            current.version += 1
        guard.mark_unknown()

    after = {"status": current.status, "version": current.version}
    write_audit(
        session,
        actor_id="buyer",
        action=CMD_PLACE_ORDER,
        entity_type="purchase_order",
        entity_id=current.id,
        before_state=before,
        after_state=after,
        request_id=request_id,
        idempotency_operation_id=guard.operation.id if guard.operation else None,
    )
    return {
        "purchase_order_id": current.id,
        "status": current.status,
        "attempt_no": attempt_row.attempt_no,
        "attempt_status": attempt_row.status,
        "external_order_no": attempt_row.external_order_no,
    }


def _explicit_failure_error():
    from app.errors import ConflictError

    return ConflictError("供应商明确返回失败（未创建订单），采购单回到 po_created")


def place_order(
    session: Session,
    *,
    po_id: str,
    actor_id: str,
    idempotency_key: str,
    adapter: SupplierAdapter,
    request_id: str | None = None,
) -> dict:
    """下达采购单：先持久化 attempt + ordering 并提交，再外呼供应商，最后写回。"""
    from app.services.access import require_roles

    require_roles(session, actor_id, "buyer")

    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFoundError(f"采购单不存在 {po_id}")
    if po.status != PO_CREATED:
        raise InvalidStateTransitionError(f"只有 po_created 采购单可以下达，当前状态 {po.status}")
    lines = session.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po_id)).all()
    pairs = [(po.warehouse_id, line.product_id) for line in lines]
    acquire_warehouse_product_locks(session, pairs)

    payload = {
        "po_id": po_id,
        "supplier_id": po.supplier_id,
        "warehouse_id": po.warehouse_id,
    }
    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_PLACE_ORDER,
        aggregate_ref=po_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        return begin.response
    if begin.action == "replay_failure":
        replay_failure(begin)

    attempt = _create_attempt_and_ordering(session, po, idempotency_key)
    session.commit()

    # 事务外调用供应商（先持久化、后外呼、再回写）
    supplier_payload = {
        "supplier_id": po.supplier_id,
        "warehouse_id": po.warehouse_id,
        "purchase_order_id": po.id,
        "lines": [
            {
                "product_id": line.product_id,
                "order_qty": line.order_qty,
                "unit_price": str(line.unit_price),
            }
            for line in lines
        ],
    }
    try:
        result = adapter.place_order(attempt.supplier_idempotency_key, supplier_payload)
    except Exception as exc:  # 断连/超时/异常：视为歧义，进入 order_unknown
        result = SupplierResult(outcome="unknown", raw=f"transport error: {exc!r}")

    from app.db import get_session_factory

    factory = get_session_factory()
    with factory() as write_session:
        try:
            outcome = _write_back(
                write_session,
                po=po,
                attempt=attempt,
                guard=guard,
                result=result,
                request_id=request_id,
            )
            write_session.commit()
        except Exception:
            write_session.rollback()
            raise
    return outcome


# ---------------------------------------------------------------- 未知状态恢复


def query_unknown_order(
    session: Session,
    *,
    po_id: str,
    actor_id: str,
    idempotency_key: str,
    adapter: SupplierAdapter,
    request_id: str | None = None,
    allow_stale_ordering: bool = False,
) -> dict:
    """查询未知订单：沿用原供应商幂等键，先查询外部事实再决定迁移。

    buyer 手动触发与 system 后台恢复共用本服务（需求 7 / 架构 9）。
    """
    from app.services.access import require_roles

    require_roles(session, actor_id, "buyer", "system")

    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFoundError(f"采购单不存在 {po_id}")
    if po.status == PO_ORDERING and not allow_stale_ordering:
        raise InvalidStateTransitionError("采购单仍处于 ordering（进行中），请先等待超时或由后台恢复任务处理")
    if po.status not in (PO_ORDERING, PO_ORDER_UNKNOWN):
        raise InvalidStateTransitionError(f"只有 ordering/order_unknown 采购单可以查询恢复，当前状态 {po.status}")

    attempt = session.scalar(
        select(PurchaseOrderAttempt)
        .where(PurchaseOrderAttempt.purchase_order_id == po_id)
        .order_by(PurchaseOrderAttempt.attempt_no.desc())
    )
    if attempt is None:  # pragma: no cover
        raise ConflictError("采购单没有下单尝试记录，无法查询外部事实")

    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_QUERY_ORDER,
        aggregate_ref=po_id,
        idempotency_key=idempotency_key,
        payload={"po_id": po_id, "supplier_key": attempt.supplier_idempotency_key},
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        return begin.response
    if begin.action == "replay_failure":
        replay_failure(begin)

    query = adapter.query_order(attempt.supplier_idempotency_key)
    before = {"status": po.status, "version": po.version}

    if query.found:
        _po_transition(po, PO_ORDERED)
        po.status = PO_ORDERED
        po.version += 1
        attempt.status = ATTEMPT_QUERIED_CREATED
        attempt.external_order_no = query.external_order_no
        outcome_status = PO_ORDERED
    else:
        _po_transition(po, PO_CREATED)
        po.status = PO_CREATED
        po.version += 1
        attempt.status = ATTEMPT_QUERIED_NOT_FOUND
        outcome_status = PO_CREATED

    guard.succeed(
        {
            "status": outcome_status,
            "external_order_no": query.external_order_no,
            "attempt_no": attempt.attempt_no,
        },
        entity_type="purchase_order",
        entity_id=po.id,
    )
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_QUERY_ORDER,
        entity_type="purchase_order",
        entity_id=po.id,
        before_state=before,
        after_state={"status": po.status, "version": po.version},
        request_id=request_id,
        idempotency_operation_id=guard.operation.id if guard.operation else None,
    )
    return {
        "purchase_order_id": po.id,
        "status": po.status,
        "external_order_no": query.external_order_no,
    }


def recover_stale_ordering(
    session: Session, *, po_id: str, adapter: SupplierAdapter, request_id: str | None = None
) -> dict:
    """后台恢复：陈旧 ordering 先转 order_unknown，再用原幂等键查询（宪法第十四条）。"""
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFoundError(f"采购单不存在 {po_id}")
    if po.status == PO_ORDERING:
        _po_transition(po, PO_ORDER_UNKNOWN)
        po.status = PO_ORDER_UNKNOWN
        po.version += 1
        session.commit()
    return query_unknown_order(
        session,
        po_id=po_id,
        actor_id="system",
        idempotency_key=f"recover:{po_id}:{po.version}",
        adapter=adapter,
        request_id=request_id,
        allow_stale_ordering=True,
    )


# ---------------------------------------------------------------- 收货


def _all_lines_received(session: Session, po: PurchaseOrder) -> bool:
    """PO 全部"有数量"明细是否都已收齐（需求 6.3：全部收齐才 received）。

    只统计 order_qty > 0 的明细：净需求为 0 的明细不产生收货义务，
    不应阻止 PO 进入 received（历史数据可能含 0 数量行）。
    """
    lines = session.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id)).all()
    active_lines = [line for line in lines if line.order_qty > 0]
    return bool(active_lines) and all(line.received_qty >= line.order_qty for line in active_lines)


def receive_goods(
    session: Session,
    *,
    po_line_id: str,
    receipt_event_id: str,
    qty: int,
    actor_id: str,
    idempotency_key: str,
    request_id: str | None = None,
) -> dict:
    """分批收货（需求 6.3 / 架构 7.3）。

    - receipt_event_id 全局唯一：同载荷重复返回原结果；异载荷复用 -> RECEIPT_EVENT_REUSED；
    - 单次/累计收货量不能超过采购单剩余量；
    - 收货事务原子完成：收货事件、库存移动、库存量、累计收货、采购单状态/版本、审计。
    """
    from app.services.access import require_roles

    require_roles(session, actor_id, "operator", "buyer")

    if qty <= 0:
        raise ValidationError(f"收货数量必须大于 0，收到 {qty}")

    payload = {"po_line_id": po_line_id, "receipt_event_id": receipt_event_id, "qty": qty}
    payload_hash_value = payload_hash(payload)

    existing = session.scalar(select(ReceiptEvent).where(ReceiptEvent.receipt_event_id == receipt_event_id))
    if existing is not None:
        if existing.payload_hash == payload_hash_value:
            return {
                "receipt_event_id": receipt_event_id,
                "status": "duplicate_replay",
                "received_qty": existing.qty,
            }
        raise ReceiptEventReusedError(
            "相同 receipt_event_id 但载荷不同，拒绝复用",
            detail={"receipt_event_id": receipt_event_id},
        )

    line = session.get(PurchaseOrderLine, po_line_id, with_for_update=True)
    if line is None:
        raise NotFoundError(f"采购单明细不存在 {po_line_id}")
    po = session.get(PurchaseOrder, line.purchase_order_id, with_for_update=True)
    if po is None:  # pragma: no cover
        raise NotFoundError(f"采购单不存在 {line.purchase_order_id}")
    if po.status not in (PO_ORDERED, PO_PARTIALLY_RECEIVED):
        raise InvalidStateTransitionError(f"只有 ordered/partially_received 采购单可以收货，当前状态 {po.status}")

    remaining = line.order_qty - line.received_qty
    if qty > remaining:
        raise ReceiptExceedsRemainingError(
            f"收货数量 {qty} 超过剩余量 {remaining}",
            detail={"remaining": remaining},
        )

    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_RECEIVE,
        aggregate_ref=po_line_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        return begin.response
    if begin.action == "replay_failure":
        replay_failure(begin)

    # 锁定库存单元并原子更新（串行化并发收货）
    quants = session.scalars(
        select(Quant)
        .where(
            Quant.warehouse_id == po.warehouse_id,
            Quant.product_id == line.product_id,
        )
        .with_for_update()
    ).all()
    if not quants:  # pragma: no cover
        raise ConflictError(f"缺少库存单元 warehouse={po.warehouse_id} product={line.product_id}")
    first = quants[0]
    first.on_hand += qty
    first.version += 1

    before = {"po_status": po.status, "po_version": po.version, "received_qty": line.received_qty}

    receipt = ReceiptEvent(
        receipt_event_id=receipt_event_id,
        purchase_order_line_id=line.id,
        warehouse_id=po.warehouse_id,
        product_id=line.product_id,
        qty=qty,
        payload_hash=payload_hash_value,
        hash_schema_version=HASH_SCHEMA_VERSION,
        actor_id=actor_id,
    )
    session.add(receipt)
    session.add(
        # StockMove 通过 ORM 直插（qty>0 入库）
        _stock_move(
            warehouse_id=po.warehouse_id,
            product_id=line.product_id,
            location_id=first.location_id,
            lot_id=first.lot_id,
            qty=qty,
            move_type="incoming",
            ref_type="receipt",
            ref_id=line.id,
            receipt_event_id=receipt_event_id,
        )
    )
    line.received_qty += qty
    line.version += 1
    if line.received_qty == line.order_qty and _all_lines_received(session, po):
        # 需求 6.3：PO 仅在"全部明细收齐"时才 received；多行 PO 收满一行仍为 partially_received
        _po_transition(po, PO_RECEIVED)
        po.status = PO_RECEIVED
    else:
        _po_transition(po, PO_PARTIALLY_RECEIVED)
        po.status = PO_PARTIALLY_RECEIVED
    po.version += 1

    guard.succeed(
        {
            "receipt_event_id": receipt_event_id,
            "po_line_id": line.id,
            "qty": qty,
            "received_qty": line.received_qty,
            "po_status": po.status,
        },
        entity_type="purchase_order_line",
        entity_id=line.id,
    )
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_RECEIVE,
        entity_type="purchase_order",
        entity_id=po.id,
        before_state=before,
        after_state={"po_status": po.status, "received_qty": line.received_qty},
        request_id=request_id,
        idempotency_operation_id=guard.operation.id if guard.operation else None,
    )
    return {
        "receipt_event_id": receipt_event_id,
        "po_line_id": line.id,
        "qty": qty,
        "received_qty": line.received_qty,
        "po_status": po.status,
    }


def _stock_move(
    *,
    warehouse_id,
    product_id,
    location_id,
    lot_id,
    qty,
    move_type,
    ref_type,
    ref_id,
    receipt_event_id,
):
    from app.models.inventory import StockMove

    return StockMove(
        warehouse_id=warehouse_id,
        product_id=product_id,
        location_id=location_id,
        lot_id=lot_id,
        qty=qty,
        move_type=move_type,
        ref_type=ref_type,
        ref_id=ref_id,
        receipt_event_id=receipt_event_id,
    )


# ---------------------------------------------------------------- 关闭 / 取消


def close_purchase_order(
    session: Session,
    *,
    po_id: str,
    actor_id: str,
    idempotency_key: str,
    request_id: str | None = None,
) -> dict:
    """received -> closed（buyer 明确确认关闭）。"""
    from app.services.access import require_roles

    require_roles(session, actor_id, "buyer")
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFoundError(f"采购单不存在 {po_id}")
    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_CLOSE_PO,
        aggregate_ref=po_id,
        idempotency_key=idempotency_key,
        payload={"po_id": po_id, "status": po.status},
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        return begin.response
    if begin.action == "replay_failure":
        replay_failure(begin)

    _po_transition(po, PO_CLOSED)
    before = {"status": po.status, "version": po.version}
    po.status = PO_CLOSED
    po.version += 1
    guard.succeed(
        {"purchase_order_id": po.id, "status": po.status},
        entity_type="purchase_order",
        entity_id=po.id,
    )
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_CLOSE_PO,
        entity_type="purchase_order",
        entity_id=po.id,
        before_state=before,
        after_state={"status": po.status, "version": po.version},
        request_id=request_id,
        idempotency_operation_id=guard.operation.id if guard.operation else None,
    )
    return {"purchase_order_id": po.id, "status": po.status}


def cancel_purchase_order(
    session: Session,
    *,
    po_id: str,
    actor_id: str,
    idempotency_key: str,
    request_id: str | None = None,
) -> dict:
    """仅取消尚未开始外部下单的 po_created（需求 6.2：无进行中/未知/成功外部尝试）。"""
    from app.services.access import require_roles

    require_roles(session, actor_id, "buyer")
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFoundError(f"采购单不存在 {po_id}")
    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_CANCEL_PO,
        aggregate_ref=po_id,
        idempotency_key=idempotency_key,
        payload={"po_id": po_id, "status": po.status},
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        return begin.response
    if begin.action == "replay_failure":
        replay_failure(begin)

    _po_transition(po, PO_CANCELLED)
    # 守卫：不存在进行中/未知/成功的外部下单尝试
    attempts = session.scalars(
        select(PurchaseOrderAttempt).where(PurchaseOrderAttempt.purchase_order_id == po_id)
    ).all()
    if any(a.status in (ATTEMPT_ATTEMPTING, ATTEMPT_SUCCEEDED, ATTEMPT_UNKNOWN) for a in attempts):
        raise InvalidStateTransitionError("存在进行中/未知/成功的外部下单尝试，禁止取消；V1 不提供已下达订单取消")

    before = {"status": po.status, "version": po.version}
    po.status = PO_CANCELLED
    po.version += 1
    guard.succeed(
        {"purchase_order_id": po.id, "status": po.status},
        entity_type="purchase_order",
        entity_id=po.id,
    )
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_CANCEL_PO,
        entity_type="purchase_order",
        entity_id=po.id,
        before_state=before,
        after_state={"status": po.status, "version": po.version},
        request_id=request_id,
        idempotency_operation_id=guard.operation.id if guard.operation else None,
    )
    return {"purchase_order_id": po.id, "status": po.status}


def list_purchase_orders(
    session: Session, *, warehouse_id: str | None = None, status: str | None = None
) -> list[PurchaseOrder]:
    stmt = select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc())
    if warehouse_id:
        stmt = stmt.where(PurchaseOrder.warehouse_id == warehouse_id)
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    return list(session.scalars(stmt).all())
