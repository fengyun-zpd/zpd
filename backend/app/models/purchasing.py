"""采购域模型：供应商、SKU-供应商关系、采购单、采购单明细、下单尝试记录、收货事件。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import MONEY_SCALE, NUMERIC_PRECISION
from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Supplier(Base):
    __tablename__ = "supplier"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    business_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SupplierProduct(Base):
    """SKU-供应商关系（供货能力与约束）。"""

    __tablename__ = "supplier_product"
    __table_args__ = (
        UniqueConstraint("supplier_id", "product_id", name="uq_supplier_product"),
        CheckConstraint("price >= 0", name="ck_sp_price_nonneg"),
        CheckConstraint("lead_days >= 0", name="ck_sp_lead_days_nonneg"),
        CheckConstraint("minimum_order_qty >= 0", name="ck_sp_min_qty_nonneg"),
        CheckConstraint("pack_multiple > 0", name="ck_sp_pack_multiple_pos"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_sp_effective_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("supplier.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    business_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    price: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, MONEY_SCALE), nullable=False)
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_order_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pack_multiple: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 关系版本（进入哈希）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PurchaseOrder(Base):
    __tablename__ = "purchase_order"
    __table_args__ = (
        CheckConstraint(
            "status in ('po_created','ordering','ordered','order_unknown',"
            "'partially_received','received','closed','cancelled')",
            name="ck_po_status_enum",
        ),
        CheckConstraint("currency = 'CNY'", name="ck_po_currency_cny"),
        Index("ix_po_plan", "plan_id"),
        Index("ix_po_supplier_status", "supplier_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    warehouse_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("supplier.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="po_created")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_line"
    __table_args__ = (
        # 一个批准明细最多落实一次（需求 6.1.1）
        UniqueConstraint("plan_line_id", name="uq_po_line_plan_line"),
        CheckConstraint("order_qty >= 0", name="ck_po_line_qty_nonneg"),
        CheckConstraint("received_qty >= 0", name="ck_po_line_received_nonneg"),
        CheckConstraint("received_qty <= order_qty", name="ck_po_line_received_le_order"),
        CheckConstraint("unit_price >= 0", name="ck_po_line_price_nonneg"),
        Index("ix_po_line_po", "purchase_order_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_order.id", ondelete="CASCADE"), nullable=False)
    plan_line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_qty: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_qty: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(NUMERIC_PRECISION, MONEY_SCALE), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    purchase_order: Mapped[PurchaseOrder] = relationship()


class PurchaseOrderAttempt(Base):
    """不可变下单尝试记录（未知状态恢复证据）。"""

    __tablename__ = "purchase_order_attempt"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "attempt_no", name="uq_po_attempt_no"),
        UniqueConstraint("supplier_idempotency_key", name="uq_po_attempt_supplier_key"),
        CheckConstraint(
            "status in ('attempting','succeeded','failed','unknown','queried_created','queried_not_found')",
            name="ck_attempt_status_enum",
        ),
        Index("ix_attempt_po", "purchase_order_id"),
        Index("ix_attempt_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_order.id", ondelete="CASCADE"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    supplier_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="attempting")
    external_order_no: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReceiptEvent(Base):
    """收货事件（receipt_event_id 全局唯一防重）。"""

    __tablename__ = "receipt_event"
    __table_args__ = (
        UniqueConstraint("receipt_event_id", name="uq_receipt_event_id"),
        CheckConstraint("qty > 0", name="ck_receipt_qty_pos"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    receipt_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purchase_order_line_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    warehouse_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    qty: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
