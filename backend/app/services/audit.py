"""审计日志服务（需求 11 / 架构 11：不记录密钥与完整 Prompt）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.governance import AuditLog


def write_audit(
    session: Session,
    *,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    error_code: str | None = None,
    request_id: str | None = None,
    idempotency_operation_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=before_state,
        after_state=after_state,
        error_code=error_code,
        request_id=request_id,
        idempotency_operation_id=idempotency_operation_id,
    )
    session.add(entry)
    return entry
