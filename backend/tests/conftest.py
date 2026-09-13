"""pytest 配置与共享夹具。

- 纯单元/性质测试不依赖数据库；
- 依赖数据库的测试使用 stockmind_test 库（存在时），不存在则整体跳过并说明原因；
- 测试前必须可复现：固定随机种子、逐会话重建 schema 与种子数据。
"""

from __future__ import annotations

import os


def _ensure_connect_timeout(dsn: str, seconds: int = 5) -> str:
    """给 DSN 补上 ``connect_timeout``。

    没有超时时，数据库不可达会让 psycopg 长时间等待，pytest 在 **collection 阶段**
    就卡住（表现为"无输出超时"），掩盖真实原因；补上超时后会在数秒内如实判定不可用。
    """
    if "connect_timeout" in dsn:
        return dsn
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}connect_timeout={seconds}"


# 测试环境：在导入 app 模块前设置（config 带 lru_cache）
TEST_DSN = _ensure_connect_timeout(
    os.environ.get(
        "TEST_POSTGRES_DSN",
        "postgresql+psycopg://stockmind:stockmind@127.0.0.1:5432/stockmind_test",
    )
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

# 必须显式导入全部 ORM 模型：否则 Base.metadata 为空，create_all 会静默建 0 张表，
# 表现为 `relation "warehouse" does not exist`（只有其他测试文件恰好 import 过模型时
# 才不暴露 —— 这正是集成测试按文件组合偶发失败的根因）。
import app.models  # noqa: E402,F401
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


def _sync_app_engine() -> None:
    """让 app 侧的全局 engine / 配置缓存与刚重建的测试 schema 对齐。

    ``DROP SCHEMA ... CASCADE`` 会让**重建之前**建立的连接看不到任何表；而 app 侧的
    ``get_session_factory()`` 是进程级单例并持有连接池。若不重置，API / Agent 测试就会
    偶发 ``relation "warehouse" does not exist``（集成测试不稳定的根因）。因此每次重建
    schema 之后都必须丢弃旧连接池并重新绑定配置。
    """
    os.environ["POSTGRES_DSN"] = TEST_DSN

    from app.config import get_settings

    get_settings.cache_clear()
    reset_engine()


@pytest.fixture()
def db_session(db_session_factory):
    """每个测试重建 schema 与种子数据（固定随机种子、串行、隔离、可重复）。

    固定顺序：DROP SCHEMA → CREATE SCHEMA → 扩展 → 建表 → **同步 app engine** → 种子。
    同步 app engine 是关键一步，见 ``_sync_app_engine``。
    """
    session = db_session_factory()
    try:
        session.execute(text("DROP SCHEMA public CASCADE"))
        session.execute(text("CREATE SCHEMA public"))
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.commit()
        # 防御性校验：模型未注册时 create_all 会静默建 0 张表，后续 seed 才报
        # `relation "warehouse" does not exist`（晦涩且难定位）。这里立即失败并说明原因。
        if not Base.metadata.tables:  # pragma: no cover 防御
            raise RuntimeError("ORM 模型未注册（Base.metadata 为空）：请确认 conftest 已 import app.models")
        Base.metadata.create_all(session.get_bind())
        if session.execute(text("select to_regclass('warehouse')")).scalar() is None:  # pragma: no cover 防御
            raise RuntimeError("测试 schema 建表失败：warehouse 表不存在（请检查迁移/模型注册）")

        from app.seed.seed import run_seed

        run_seed(session, reset=False)
        # 种子就绪后再同步 app 侧 engine：确保测试体内 app 使用重建后的新连接池，
        # 同时不打断上面建表/播种所使用的连接（顺序颠倒会导致 seed 报 relation 不存在）。
        _sync_app_engine()
        yield session
    finally:
        session.close()
        # 测试结束后同样重置：避免连接池把上一个测试的 schema 连接带进下一个测试
        _sync_app_engine()


@pytest.fixture()
def seeded_session(db_session):
    return db_session
