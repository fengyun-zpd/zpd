"""未知订单恢复任务（需求 7 / 架构 9 / 宪法第十四条）。

- order_unknown：沿用原供应商幂等键查询外部事实，确认后再迁移；
- 陈旧 ordering（超过配置超时）：先转 order_unknown，再查询；
- 禁止盲目重试或换键重下；单个采购单失败不影响其他采购单。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.constants import PO_ORDER_UNKNOWN, PO_ORDERING
from app.models.purchasing import PurchaseOrder
from app.services.purchase_service import query_unknown_order
from app.tasks.celery_app import celery

logger = logging.getLogger("stockmind.tasks.recovery")


@celery.task(name="stockmind.recover_unknown_orders", bind=True)
def recover_unknown_orders(self) -> dict:
    """后台恢复：system 主体，查询未知订单并把陈旧 ordering 收敛到 order_unknown。"""
    from app.db import get_session_factory

    settings = get_settings()
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=settings.order_timeout_seconds)

    factory = get_session_factory()
    results = {"recovered": 0, "failed": 0}
    with factory() as session:
        candidates = session.scalars(
            select(PurchaseOrder).where(
                PurchaseOrder.status.in_([PO_ORDER_UNKNOWN, PO_ORDERING]),
            )
        ).all()
        for po in candidates:
            if po.status == PO_ORDERING and po.updated_at and po.updated_at > stale_before:
                continue  # 仍在超时窗口内
            try:
                outcome = query_unknown_order(
                    session,
                    po_id=po.id,
                    actor_id="system",
                    idempotency_key=f"system-recover:{po.id}:{po.version}",
                    adapter=_adapter(),
                    allow_stale_ordering=True,
                )
                session.commit()
                results["recovered"] += 1
                logger.info("recovered po=%s -> %s", po.id, outcome["status"])
            except Exception as exc:  # noqa: BLE001 单 PO 失败不影响其他
                session.rollback()
                results["failed"] += 1
                logger.warning("recovery failed po=%s: %s", po.id, exc)
    return results


def _adapter():
    from app.supplier_adapter import HttpSupplierAdapter

    return HttpSupplierAdapter()
