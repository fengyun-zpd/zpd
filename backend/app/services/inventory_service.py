"""库存查询与盘点服务（只读计算输入 + 收货写入由采购/库存服务完成）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import INBOUND_PO_STATES
from app.models.inventory import DemandDayRecord, Quant
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.services.forecast import DemandDay


def get_quant_summary(session: Session, warehouse_id: str, product_id: str) -> tuple[int, int, int]:
    """返回 (on_hand, reserved, version)；version 为该 (仓库, SKU) 快照版本。"""
    rows = session.scalars(
        select(Quant).where(Quant.warehouse_id == warehouse_id, Quant.product_id == product_id)
    ).all()
    on_hand = sum(r.on_hand for r in rows)
    reserved = sum(r.reserved for r in rows)
    version = max((r.version for r in rows), default=1)
    return on_hand, reserved, version


def get_inbound_remaining(session: Session, warehouse_id: str, product_id: str) -> int:
    """有效在途量：po_created/ordering/ordered/order_unknown/partially_received 的剩余未收量。"""
    stmt = (
        select(func.coalesce(func.sum(PurchaseOrderLine.order_qty - PurchaseOrderLine.received_qty), 0))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(
            PurchaseOrder.warehouse_id == warehouse_id,
            PurchaseOrderLine.product_id == product_id,
            PurchaseOrder.status.in_(INBOUND_PO_STATES),
        )
    )
    return int(session.scalar(stmt) or 0)


def get_demand_series(
    session: Session,
    warehouse_id: str,
    product_id: str,
    *,
    upto: date,
    lookback_days: int = 120,
) -> list[DemandDay]:
    """取 upto 日（含）之前最近 lookback_days 个完整日的需求序列（升序）。

    source_complete=False 的日期保留在序列中，由预测服务截断处理。
    """
    start = upto - timedelta(days=lookback_days)
    rows = session.scalars(
        select(DemandDayRecord)
        .where(
            DemandDayRecord.warehouse_id == warehouse_id,
            DemandDayRecord.product_id == product_id,
            DemandDayRecord.day >= start,
            DemandDayRecord.day <= upto,
        )
        .order_by(DemandDayRecord.day)
    ).all()
    return [DemandDay(day=r.day, demand=r.demand, source_complete=r.source_complete) for r in rows]


def business_date(warehouse_timezone: str, *, today: date | None = None) -> date:
    """仓库业务日：按仓库时区取当前日期（缺省为今天）的前一完整业务日。

    V1 种子数据保证业务日（昨日）及之前 90-180 天需求完整。
    """
    import zoneinfo

    if today is None:
        now_local = datetime.now(zoneinfo.ZoneInfo(warehouse_timezone))
        today = now_local.date()
    return today - timedelta(days=1)
