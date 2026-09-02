"""知识库域模型：文档、分块、向量（pgvector）、规则血缘。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Document(Base):
    """知识文档（供人阅读与引用）。"""

    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            "doc_type in ('replenishment_policy','safety_stock','warehouse_rule',"
            "'supplier_constraint','receiving_sop')",
            name="ck_document_type_enum",
        ),
        CheckConstraint("version >= 1", name="ck_document_version_pos"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_document_effective_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Chunk(Base):
    __tablename__ = "chunk"
    __table_args__ = (
        UniqueConstraint("document_id", "seq_no", name="uq_chunk_doc_seq"),
        CheckConstraint("seq_no >= 1", name="ck_chunk_seq_pos"),
        # FTS 索引：中文先由 jieba 分词写入 search_text，再按空格词元建 GIN 索引
        Index(
            "ix_chunk_fts",
            text("to_tsvector('simple', search_text)"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True)
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    @property
    def source_chunk_id(self) -> str:
        """稳定业务 id：doc::chunk，供引用映射与规则血缘。"""
        return f"{self.document_id}::{self.seq_no}"


class ChunkEmbedding(Base):
    """分块向量（pgvector）。"""

    __tablename__ = "chunk_embedding"
    __table_args__ = (
        UniqueConstraint("chunk_id", name="uq_chunk_embedding_chunk"),
        Index(
            "ix_chunk_embedding_vector",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunk.id", ondelete="CASCADE"), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=384)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RuleSource(Base):
    """结构化规则与文档分块的来源血缘。"""

    __tablename__ = "rule_source"
    __table_args__ = (UniqueConstraint("rule_id", "source_chunk_id", name="uq_rule_source_chunk"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    extracted_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
