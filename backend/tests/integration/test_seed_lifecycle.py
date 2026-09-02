"""种子生命周期回归测试：默认启动不得覆盖已有业务状态。"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.inventory import Warehouse
from app.seed.seed import seed_if_empty


def test_seed_if_empty_preserves_existing_business_data(db_session):
    before = db_session.scalar(select(func.count()).select_from(Warehouse))
    result = seed_if_empty(db_session)
    after = db_session.scalar(select(func.count()).select_from(Warehouse))

    assert result is None
    assert after == before
