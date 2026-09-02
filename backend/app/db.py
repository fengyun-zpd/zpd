"""数据库引擎、会话工厂与声明式基类。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """全部 ORM 模型的声明式基类。"""


def build_engine(dsn: str | None = None) -> Engine:
    """构造引擎；测试可通过 dsn 覆盖。"""
    settings = get_settings()
    url = dsn or settings.postgres_dsn
    # pool_pre_ping 保证 Celery 长任务中连接存活
    return create_engine(url, pool_pre_ping=True, future=True)


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


def session_scope() -> Iterator[Session]:
    """请求/任务级会话上下文。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """清空全局引擎（测试隔离用）。"""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
