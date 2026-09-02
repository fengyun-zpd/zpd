"""权限单元测试（需求 1 / 架构 10 / 宪法第九条）。"""

from __future__ import annotations

import pytest

from app.errors import ForbiddenError
from app.services.access import ensure_not_browser_system, get_user_roles, require_roles


@pytest.mark.db
def test_roles_from_seeded_users(db_session):
    assert "operator" in get_user_roles(db_session, "bob")
    assert "approver" in get_user_roles(db_session, "carol")
    assert "buyer" in get_user_roles(db_session, "dave")
    assert "admin" in get_user_roles(db_session, "eve")
    assert "system" in get_user_roles(db_session, "system")


@pytest.mark.db
def test_require_roles_pass(db_session):
    require_roles(db_session, "alice", "admin")  # alice 拥有全部演示角色


@pytest.mark.db
def test_require_roles_fail(db_session):
    with pytest.raises(ForbiddenError):
        require_roles(db_session, "bob", "approver")


@pytest.mark.db
def test_unknown_actor_forbidden(db_session):
    with pytest.raises(ForbiddenError):
        get_user_roles(db_session, "hacker")


def test_browser_cannot_impersonate_system():
    with pytest.raises(ForbiddenError):
        ensure_not_browser_system("system")
    ensure_not_browser_system("alice")  # 正常用户不受影响
