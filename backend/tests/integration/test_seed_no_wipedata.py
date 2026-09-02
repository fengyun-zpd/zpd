"""验收回归：API/容器重启不得自动清空已有业务数据（说明书 §8）。

Bug 风险：若启动命令误带 --reset 或 seed 默认 reset=True，则每次重启都会
清空业务数据。当前 seed.main 默认 seed_if_empty（有数据跳过），必须保持。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.inventory import Warehouse
from app.seed.seed import seed_if_empty


def test_seed_if_empty_skips_when_data_exists(db_session):
    """已有业务数据时 seed_if_empty 必须跳过，不覆盖、不删除。"""
    # 库中已有种子数据（db_session fixture 已 run_seed）
    before = db_session.scalar(select(Warehouse.id).limit(1))
    assert before is not None

    # 手工插入一条"业务数据"标记（模拟用户已产生的数据）
    from app.models.governance import User

    marker = User(id=f"user-marker-{uuid.uuid4().hex[:8]}", display_name="marker", roles=["operator"])
    db_session.add(marker)
    db_session.commit()

    # seed_if_empty 应返回 None（跳过）且不删除 marker
    result = seed_if_empty(db_session)
    assert result is None, "已有数据时 seed_if_empty 不应重新播种"
    db_session.commit()
    assert db_session.get(User, marker.id) is not None, "seed_if_empty 不得删除已有数据"


def test_seed_reset_requires_explicit_flag(db_session):
    """run_seed(reset=True) 才清空重建；无 --reset 的启动路径不破坏已有数据。

    说明：run_seed(reset=False) 假定空库（逐表插入种子），对已含数据的库会因
    种子记录重复而 IntegrityError——这正是"启动命令不应带 reset"的原因。
    本测试验证：仅通过 seed_if_empty（默认启动路径）时数据保留。
    """
    from app.models.governance import User

    marker = User(id=f"user-marker-{uuid.uuid4().hex[:8]}", display_name="marker", roles=["operator"])
    db_session.add(marker)
    db_session.commit()
    marker_id = marker.id

    # 默认启动路径：seed_if_empty 跳过（已有数据），数据保留
    result = seed_if_empty(db_session)
    assert result is None
    db_session.commit()
    assert db_session.get(User, marker_id) is not None, "默认启动路径不得清空已有数据"
