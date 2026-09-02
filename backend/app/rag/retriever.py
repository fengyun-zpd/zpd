"""混合检索器（需求 8.1 / 架构 5）。

向量（pgvector 余弦）+ 关键词（jieba+FTS）双路召回，RRF 融合，再过滤启用/版本/范围。
检索文本一律按不可信数据对待：引用必须映射到真实启用分块；无可靠来源时如实拒答。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.knowledge import Chunk, Document
from app.rag import fts
from app.rag.fusion import rrf_merge


@dataclass
class ChunkHit:
    chunk_id: str
    source_chunk_id: str
    document_id: str
    document_title: str
    content: str
    score: float


def _vector_search(
    session: Session, query_vector: list[float], *, top_k: int = 8, limit_docs: bool = True
) -> list[tuple[str, float]]:
    """pgvector 余弦相似度召回（ORM 查询，Vector 类型由 SQLAlchemy 自动绑定）。"""
    from sqlalchemy import select

    from app.models.knowledge import Chunk, ChunkEmbedding, Document

    distance = ChunkEmbedding.embedding.cosine_distance(query_vector)
    stmt = select(ChunkEmbedding.chunk_id, (1 - distance).label("sim"))
    if limit_docs:
        stmt = stmt.join(Chunk, Chunk.id == ChunkEmbedding.chunk_id)
        stmt = stmt.join(Document, Document.id == Chunk.document_id)
        stmt = stmt.where(Document.enabled.is_(True))
    stmt = stmt.order_by(distance).limit(top_k)
    rows = session.execute(stmt).all()
    return [(row.chunk_id, float(row.sim)) for row in rows]


def retrieve(
    session: Session,
    query: str,
    *,
    query_vector: list[float] | None = None,
    top_k: int = 8,
    limit_docs: bool = True,
) -> list[ChunkHit]:
    """混合检索：向量 + 关键词 -> RRF -> 分块详情。"""
    ranked: list[list[tuple[str, float]]] = []

    if query_vector is not None:
        try:
            ranked.append(_vector_search(session, query_vector, top_k=top_k, limit_docs=limit_docs))
        except Exception:
            ranked.append([])  # 向量路径不可用时仅使用关键词路径

    try:
        keyword_hits = fts.keyword_search(session, query, top_k=top_k, limit_docs=limit_docs)
        ranked.append([(c.id, s) for c, s in keyword_hits])
    except Exception:
        ranked.append([])

    if not any(ranked):
        return []

    merged = rrf_merge(*ranked, top_k=top_k)
    hits: list[ChunkHit] = []
    for chunk_id, score in merged:
        chunk = session.get(Chunk, chunk_id)
        if chunk is None:
            continue
        doc = session.get(Document, chunk.document_id)
        if doc is None or (limit_docs and not doc.enabled):
            continue
        hits.append(
            ChunkHit(
                chunk_id=chunk.id,
                source_chunk_id=f"{chunk.document_id}::{chunk.seq_no}",
                document_id=doc.id,
                document_title=doc.title,
                content=chunk.content,
                score=score,
            )
        )
    return hits
