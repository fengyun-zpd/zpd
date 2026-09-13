"""测试夹具隔离性回归（第七部分）。

背景：``db_session`` 每个测试执行 ``DROP SCHEMA public CASCADE``，而 app 侧的
``get_session_factory()`` 是进程级单例并持有连接池。重建 schema 后若不同步重置连接池，
app 侧的陈旧连接会看到空 schema，表现为偶发 ``relation "warehouse" does not exist``。

这里固定"app 侧连接必须绑定当前 schema"的行为，并覆盖夹具依赖的重置机制。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import get_engine, get_session_factory, reset_engine


@pytest.mark.db
def test_app_engine_binds_to_current_schema(db_session):
    """app 侧连接池必须绑定当前 schema：核心表可访问且能看到本测试的种子数据。

    这正是 ``relation "warehouse" does not exist`` 的反面断言。
    """
    with get_session_factory()() as session:
        count = session.execute(text("select count(*) from warehouse")).scalar()
    assert count > 0, "app 侧必须能用当前连接读到本测试重建后的种子数据"


@pytest.mark.db
def test_fixture_and_app_see_same_seeded_data(db_session):
    """串行可重复：夹具侧与 app 侧看到同一份种子数据。"""
    fixture_count = db_session.execute(text("select count(*) from warehouse")).scalar()
    with get_session_factory()() as session:
        app_count = session.execute(text("select count(*) from warehouse")).scalar()
    assert app_count == fixture_count > 0


@pytest.mark.db
def test_reset_engine_mechanism_produces_fresh_pool(db_session):
    """``reset_engine`` 机制：重置后必须拿到新的 engine 实例（夹具同步依赖该机制）。"""
    current = get_engine()
    reset_engine()
    assert get_engine() is not current, "重置必须产生新 engine，否则陈旧连接仍指向旧 schema"
