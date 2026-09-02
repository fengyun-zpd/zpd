"""关键词检索：jieba 分词 + PostgreSQL 全文检索（需求 8.1）。"""

from __future__ import annotations

import jieba
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.knowledge import Chunk, Document

MAX_TOKENS = 24


def tokenize(text: str) -> list[str]:
    jieba.setLogLevel(60)  # 静默
    tokens = [t.strip() for t in jieba.cut(text) if t.strip() and len(t.strip()) > 1]
    return tokens[:MAX_TOKENS]


def to_search_text(text: str) -> str:
    """中文分块 -> 空格分隔词元（供 PostgreSQL FTS 建立词元索引）。"""
    return " ".join(tokenize(text))


def keyword_search(
    session: Session, query: str, *, top_k: int = 8, limit_docs: bool = True
) -> list[tuple[Chunk, float]]:
    """jieba 分词后用 OR 语义的 to_tsquery 检索分块（search_text 为空格词元），按 ts_rank 排序。"""
    tokens = tokenize(query)
    if not tokens:
        return []
    tsquery = " | ".join(tokens)
    stmt = (
        select(
            Chunk,
            func.ts_rank(
                func.to_tsvector("simple", Chunk.search_text),
                func.to_tsquery("simple", tsquery),
            ).label("rank"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(func.to_tsvector("simple", Chunk.search_text).op("@@")(func.to_tsquery("simple", tsquery)))
    )
    if limit_docs:
        stmt = stmt.where(Document.enabled.is_(True))
    stmt = stmt.order_by(
        func.ts_rank(
            func.to_tsvector("simple", Chunk.search_text),
            func.to_tsquery("simple", tsquery),
        ).desc()
    ).limit(top_k)
    return [(chunk, float(rank)) for chunk, rank in session.execute(stmt).all()]
