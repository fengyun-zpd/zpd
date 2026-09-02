"""PostgreSQL 事务级 advisory lock（需求 6.4 / 架构 7.3）。

影响同一仓库/SKU 的补货判断事务，必须按 (warehouse_id, product_id) 排序后取得
pg_advisory_xact_lock；统一排序避免多 SKU 死锁。锁超时 -> RESOURCE_BUSY。
Redis 锁只用于任务调度协调，不承担业务正确性。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ResourceBusyError


def _lock_key(session: Session, warehouse_id: str, product_id: str) -> int:
    """hashtextextended 生成稳定 64 位锁键。"""
    key = f"stockmind:{warehouse_id}:{product_id}"
    result = session.execute(text("SELECT hashtextextended(:key, 0)").bindparams(key=key)).scalar()
    assert result is not None
    return int(result)


def acquire_warehouse_product_locks(session: Session, pairs: list[tuple[str, str]]) -> None:
    """按 (warehouse_id, product_id) 字典序排序后取得事务级 advisory 锁。"""
    if not pairs:
        return
    ordered = sorted(set(pairs))
    timeout_ms = int(get_settings().advisory_lock_timeout_ms)
    # SET 语句不支持绑定参数，使用受控字面量（值来自配置整数）
    session.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    try:
        for warehouse_id, product_id in ordered:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=_lock_key(session, warehouse_id, product_id))
            )
    except OperationalError as exc:
        session.rollback()
        raise ResourceBusyError("获取 (warehouse_id, product_id) 事务锁超时，请以同一幂等键重试") from exc
