"""治理域模型：演示用户、审计日志、幂等操作记录、定时任务、执行记录、执行明细、告警。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "user"
    __table_args__ = (
        CheckConstraint(
            "roles <@ ARRAY['operator','approver','buyer','system','admin']::varchar[]",
            name="ck_user_roles_enum",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False, default=list)


class AuditLog(Base):
    """审计日志：操作者、业务动作、前后状态、错误码和关联对象；不记录密钥与完整 Prompt。"""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_actor_time", "actor_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    before_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IdempotencyOperation(Base):
    """统一幂等操作记录（需求 6.4 / 架构 7.3）。"""

    __tablename__ = "idempotency_operation"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "command_type",
            "aggregate_ref",
            "idempotency_key",
            name="uq_idem_op_scope_key",
        ),
        CheckConstraint(
            "status in ('in_progress','retryable','succeeded','failed','unknown')",
            name="ck_idem_op_status_enum",
        ),
        Index("ix_idem_op_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    principal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Schedule(Base):
    """定时任务配置（IANA 时区）。"""

    __tablename__ = "schedule"
    __table_args__ = (CheckConstraint("default_window in (7,14,30)", name="ck_schedule_window"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_window: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Execution(Base):
    """定时执行记录（同 schedule_id + scheduled_at 只创建一次）。"""

    __tablename__ = "execution"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_at", name="uq_execution_schedule_time"),
        CheckConstraint(
            "trigger_type in ('scheduled','manual','rerun')",
            name="ck_execution_trigger_enum",
        ),
        CheckConstraint(
            "status in ('running','succeeded','failed','partial')",
            name="ck_execution_status_enum",
        ),
        Index("ix_execution_schedule", "schedule_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    scanned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    draft_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    triggered_by_actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExecutionItem(Base):
    """逐 SKU 执行明细（success/blocked/failed，不阻断其他 SKU）。

    (execution_id, warehouse_id, product_id) 唯一：行级幂等键（需求 6.4）。
    """

    __tablename__ = "execution_item"
    __table_args__ = (
        CheckConstraint("status in ('success','blocked','failed')", name="ck_exec_item_status_enum"),
        UniqueConstraint("execution_id", "warehouse_id", "product_id", name="uq_exec_item_wh_product"),
        Index("ix_exec_item_execution", "execution_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    execution_id: Mapped[str] = mapped_column(ForeignKey("execution.id", ondelete="CASCADE"), nullable=False)
    warehouse_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_line_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    alert_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Alert(Base):
    """页面告警（邮件/企微放 V1.1）。同一仓库/SKU/阻断码只保留一个 open 告警。"""

    __tablename__ = "alert"
    __table_args__ = (
        # 部分唯一索引：同一仓库/SKU/阻断码最多一个 open 告警
        Index(
            "uq_alert_open_dedupe",
            "warehouse_id",
            "product_id",
            "blocker_code",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        CheckConstraint("status in ('open','closed')", name="ck_alert_status_enum"),
        Index("ix_alert_wh_product", "warehouse_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)  # blocked / failed / order_unknown ...
    warehouse_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    blocker_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
