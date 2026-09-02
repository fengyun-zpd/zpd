"""页面告警服务（需求 2.2 / 架构 8）。

同一仓库/SKU/阻断码只保留一个 open 告警（部分唯一索引）；
条件恢复后关闭告警，重新计算后方可生成有效建议。邮件/企微告警放 V1.1。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.constants import ALERT_CLOSED, ALERT_OPEN
from app.models.governance import Alert


def upsert_open_alert(
    session: Session,
    *,
    alert_type: str,
    warehouse_id: str,
    product_id: str,
    blocker_code: str,
    message: str,
    payload: dict | None = None,
) -> Alert:
    """打开/刷新一个去重告警；并发命中唯一约束时刷新既有 open 告警。"""
    values = {
        "alert_type": alert_type,
        "warehouse_id": warehouse_id,
        "product_id": product_id,
        "blocker_code": blocker_code,
        "status": ALERT_OPEN,
        "message": message,
        "payload": payload,
    }
    stmt = pg_insert(Alert).values(**values)
    stmt = stmt.on_conflict_do_update(
        # 部分唯一索引：不能 ON CONSTRAINT，需指定 index_elements + index_where；
        # 推断谓词必须是字面量（PostgreSQL 不允许参数化索引推断）
        index_elements=[Alert.warehouse_id, Alert.product_id, Alert.blocker_code],
        index_where=text("status = 'open'"),
        set_={
            "alert_type": stmt.excluded.alert_type,
            "message": stmt.excluded.message,
            "payload": stmt.excluded.payload,
        },
    )
    session.execute(stmt)
    alert = session.scalar(
        select(Alert).where(
            Alert.warehouse_id == warehouse_id,
            Alert.product_id == product_id,
            Alert.blocker_code == blocker_code,
            Alert.status == ALERT_OPEN,
        )
    )
    assert alert is not None  # upsert 后必然存在
    return alert


def close_open_alert(
    session: Session,
    *,
    warehouse_id: str,
    product_id: str,
    blocker_code: str,
) -> None:
    """条件恢复后关闭告警。"""
    session.execute(
        update(Alert)
        .where(
            Alert.warehouse_id == warehouse_id,
            Alert.product_id == product_id,
            Alert.blocker_code == blocker_code,
            Alert.status == ALERT_OPEN,
        )
        .values(status=ALERT_CLOSED, closed_at=datetime.now(timezone.utc))
    )
