"""补货域模型：补货计划、计划明细、结构化规则。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import NUMERIC_PRECISION, NUMERIC_SCALE
from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ReplenishmentRule(Base):
    """结构化规则（已校验、可进计算）。"""

    __tablename__ = "replenishment_rule"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_rule_version_pos"),
        CheckConstraint(
            "scope in ('product','category','warehouse','global')",
            name="ck_rule_scope_enum",
        ),
        CheckConstraint("safety_stock >= 0", name="ck_rule_safety_stock_nonneg"),
        CheckConstraint("review_period_days >= 0", name="ck_rule_review_days_nonneg"),
        Index("ix_rule_scope_apply", "scope", "enabled", "effective_from"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    # 作用域指向：product 规则 -> scope_product_id；category -> scope_category；warehouse -> scope_warehouse_id
    scope_product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scope_warehouse_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 数值参数（通过结构化校验后才允许进入计算）
    safety_stock: Mapped[Decimal | None] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=True)
    review_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ReplenishmentPlan(Base):
    """补货计划（头）。"""

    __tablename__ = "replenishment_plan"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft','pending_approval','approved','rejected','superseded')",
            name="ck_plan_status_enum",
        ),
        CheckConstraint(
            "requested_window in (7,14,30)",
            name="ck_plan_requested_window_enum",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    requested_window: Mapped[int] = mapped_column(Integer, nullable=False)
    planning_date: Mapped[date] = mapped_column(Date, nullable=False)  # 仓库业务日
    revision_of_plan_id: Mapped[str | None] = mapped_column(ForeignKey("replenishment_plan.id"), nullable=True)
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 乐观版本
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlanLine(Base):
    """补货计划明细（每 SKU 计算快照、证据与决策输入哈希）。"""

    __tablename__ = "plan_line"
    __table_args__ = (
        UniqueConstraint("plan_id", "product_id", name="uq_plan_line_product"),
        # 活动建议防重：同一仓库/SKU 同时最多一个 active_for_dedupe=true 的有效明细（需求 6.1.1）
        Index(
            "uq_plan_line_active_dedupe",
            "warehouse_id",
            "product_id",
            unique=True,
            postgresql_where=text("active_for_dedupe"),
        ),
        CheckConstraint(
            "flag in ('valid','excluded','blocked')",
            name="ck_plan_line_flag_enum",
        ),
        CheckConstraint("planning_window >= 0", name="ck_plan_line_window_nonneg"),
        CheckConstraint("order_qty >= 0", name="ck_plan_line_order_qty_nonneg"),
        CheckConstraint("minimum_order_qty >= 0", name="ck_plan_line_min_qty_nonneg"),
        CheckConstraint("pack_multiple > 0", name="ck_plan_line_pack_multiple_pos"),
        CheckConstraint("lead_days >= 0", name="ck_plan_line_lead_days_nonneg"),
        CheckConstraint("review_period_days >= 0", name="ck_plan_line_review_days_nonneg"),
        CheckConstraint("on_hand >= 0", name="ck_plan_line_on_hand_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_plan_line_reserved_nonneg"),
        CheckConstraint("reserved <= on_hand", name="ck_plan_line_reserved_le_on_hand"),
        Index("ix_plan_line_plan", "plan_id"),
        Index("ix_plan_line_wh_product", "warehouse_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("replenishment_plan.id", ondelete="CASCADE"), nullable=False)
    warehouse_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    flag: Mapped[str] = mapped_column(String(16), nullable=False, default="valid")
    active_for_dedupe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    blocked_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # 计算中间量与结果（预测/指标按需求 5.3.1 使用 Decimal 精度，禁止二进制浮点）
    daily_forecast: Mapped[Decimal | None] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=True)
    forecast_algorithm: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mae: Mapped[Decimal | None] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=True)
    wape: Mapped[Decimal | None] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    planning_window: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_demand: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=False)
    target_stock: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=False)
    available: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=False)
    inbound: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=False)
    net_demand: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=False)
    order_qty: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fixed_safety_stock: Mapped[Decimal] = mapped_column(
        Numeric(NUMERIC_PRECISION, NUMERIC_SCALE), nullable=False, default=Decimal("0")
    )
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minimum_order_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pack_multiple: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supplier_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    on_hand: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # 证据与新鲜度
    decision_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    intermediate: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rule_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    demand_cutoff_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    quant_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier_rel_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
