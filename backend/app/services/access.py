"""权限服务（需求 1 / 架构 10）。

V1 通过 actor_id 查询种子用户角色，不信任前端提交的角色字符串。
`system` 是 Celery Worker 使用的内部服务主体，不接受浏览器/REST 冒充。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import ROLE_SYSTEM
from app.errors import ForbiddenError
from app.models.governance import User


def get_user_roles(session: Session, actor_id: str) -> list[str]:
    user = session.get(User, actor_id)
    if user is None:
        raise ForbiddenError(f"未知演示用户 actor_id={actor_id}")
    return list(user.roles)


def require_roles(session: Session, actor_id: str, *required: str) -> None:
    """要求 actor_id 具备任意一个 required 角色。"""
    roles = get_user_roles(session, actor_id)
    if not any(role in roles for role in required):
        raise ForbiddenError(f"角色不足：需要 {required} 之一，当前 {roles}")


def ensure_not_browser_system(actor_id: str) -> None:
    """system 只能由后台内部调用，浏览器/REST 不得以 system 身份操作。"""
    if actor_id == ROLE_SYSTEM:
        raise ForbiddenError("system 是内部服务主体，禁止通过请求冒充")
