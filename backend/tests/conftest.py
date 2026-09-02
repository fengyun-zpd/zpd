"""pytest 配置与共享夹具。

- 纯单元/性质测试不依赖数据库；
- 依赖数据库的测试使用 stockmind_test 库（存在时），不存在则整体跳过并说明原因；
- 测试前必须可复现：固定随机种子、逐会话重建 schema 与种子数据。
"""

from __future__ import annotations

import os

# 测试环境：在导入 app 模块前设置（config 带 lru_cache）
TEST_DSN = os.environ.get(
    "TEST_POSTGRES_DSN",
    "postgresql+psycopg://stockmind:stockmind@127.0.0.1:5432/stockmind_test",
)
# 强制：无论外层环境（compose/.env）提供什么，测试进程所有 app factory 都指向测试库。
os.environ["POSTGRES_DSN"] = TEST_DSN
# 测试绝不调用真实 LLM（离线确定性）；schema 每测试重建，Postgres checkpoint 表随之
# 重建，故强制内存 checkpoint（避免长驻连接被 DROP SCHEMA 终止后无法自愈）。
os.environ["LLM_API_KEY"] = ""
os.environ["CHECKPOINTER_BACKEND"] = "memory"
os.environ["EMBEDDING_ENABLED"] = "false"
os.environ["MOCK_SUPPLIER_URL"] = "http://127.0.0.1:8100"
os.environ["ORDER_TIMEOUT_SECONDS"] = "5"

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, build_engine, reset_engine, sessionmaker  # noqa: E402


def _db_available() -> bool:
    try:
        engine = build_engine(TEST_DSN)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


DB_AVAILABLE = _db_available()


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    """无数据库时跳过 db 依赖测试（如实说明原因）。"""
    if DB_AVAILABLE:
        return
    for item in items:
        if "db" in item.keywords:
            item.add_marker(
                pytest.mark.skip(reason="本机未提供 PostgreSQL（TEST_POSTGRES_DSN 不可达），数据库测试未执行")
            )


@pytest.fixture(scope="session")
def db_engine():
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL 不可用")
    engine = build_engine(TEST_DSN)
    yield engine
    engine.dispose()
    reset_engine()


@pytest.fixture(scope="session")
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False, future=True)


@pytest.fixture()
def db_session(db_session_factory):
    """每个测试重建 schema 与种子数据（固定随机种子，可复现）。"""
    session = db_session_factory()
    try:
        session.execute(text("DROP SCHEMA public CASCADE"))
        session.execute(text("CREATE SCHEMA public"))
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.commit()
        Base.metadata.create_all(session.get_bind())

        from app.seed.seed import run_seed

        run_seed(session, reset=False)
        yield session
    finally:
        session.close()


@pytest.fixture()
def seeded_session(db_session):
    return db_session
