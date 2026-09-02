"""库存域模型：商品、仓库、库位、批次、库存量、库存移动。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Product(Base):
    """商品（SKU）。"""

    __tablename__ = "product"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # SKU 编码
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    basic_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="件")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Warehouse(Base):
    """仓库（V1 种子统一 Asia/Shanghai 时区）。"""

    __tablename__ = "warehouse"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Location(Base):
    __tablename__ = "location"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    warehouse_id: Mapped[str] = mapped_column(
        ForeignKey("warehouse.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Lot(Base):
    __tablename__ = "lot"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("location.id"), nullable=False)
    batch_no: Mapped[str] = mapped_column(String(128), nullable=False)


class Quant(Base):
    """库存量：现存量与预留量（V1 均为 SKU 基本单位非负整数）。"""

    __tablename__ = "quant"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "product_id", "location_id", "lot_id", name="uq_quant_cell"),
        CheckConstraint("on_hand >= 0", name="ck_quant_on_hand_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_quant_reserved_nonneg"),
        CheckConstraint("reserved <= on_hand", name="ck_quant_reserved_le_on_hand"),
        Index("ix_quant_wh_product", "warehouse_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("location.id"), nullable=False)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lot.id"), nullable=False)
    on_hand: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class DemandDayRecord(Base):
    """日需求账本：按仓库时区聚合的完整业务日事实。

    source_complete=False 表示该日来源覆盖不明或缺失，不得当作 0（需求 5.1）。
    """

    __tablename__ = "demand_day"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "product_id", "day", name="uq_demand_day_wh_product_day"),
        CheckConstraint("demand >= 0", name="ck_demand_day_nonneg"),
        Index("ix_demand_day_wh_product", "warehouse_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    demand: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StockMove(Base):
    """库存移动事实（收货、出库等）。"""

    __tablename__ = "stock_move"
    __table_args__ = (
        CheckConstraint("qty != 0", name="ck_stock_move_qty_nonzero"),
        Index("ix_stock_move_ref", "ref_type", "ref_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouse.id"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), nullable=False, index=True)
    location_id: Mapped[str] = mapped_column(ForeignKey("location.id"), nullable=False)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lot.id"), nullable=False)
    qty: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 正=入库，负=出库
    move_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
