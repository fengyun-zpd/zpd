"""本地中文 Embedding（需求 8.1 / 架构 5 / ADR 决策 2）。

使用 sentence-transformers 本地模型（默认 paraphrase-multilingual-MiniLM-L12-v2），
首次运行联网下载。模型不可用时向量路径不可用（如实报错），关键词路径仍可运行。
"""

from __future__ import annotations

import threading

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import DataUnavailableError
from app.models.knowledge import Chunk, ChunkEmbedding, Document

_lock = threading.Lock()
_model = None
_model_name: str = ""


def _get_model():
    global _model, _model_name
    settings = get_settings()
    if _model is not None and _model_name == settings.embedding_model_name:
        return _model
    with _lock:
        if _model is not None and _model_name == settings.embedding_model_name:
            return _model
        try:
            from sentence_transformers import SentenceTransformer  # 延迟导入
        except ImportError as exc:
            raise DataUnavailableError("未安装 sentence-transformers（需安装 [rag] 依赖）") from exc
        try:
            _model = SentenceTransformer(settings.embedding_model_name, device=settings.embedding_device)
        except Exception as exc:
            raise DataUnavailableError(f"Embedding 模型加载失败：{exc!r}（首次运行需联网下载）") from exc
        _model_name = settings.embedding_model_name
        return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量生成向量；未启用或模型不可用时报 DATA_UNAVAILABLE。"""
    if not get_settings().embedding_enabled:
        raise DataUnavailableError("EMBEDDING_ENABLED=false，向量路径不可用")
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


def index_all_embeddings(session: Session) -> int:
    """为全部启用文档的分块重建向量索引。"""
    settings = get_settings()
    chunks = session.scalars(
        select(Chunk).join(Document, Document.id == Chunk.document_id).where(Document.enabled.is_(True))
    ).all()
    if not chunks:
        return 0
    vectors = embed_texts([c.content for c in chunks])
    session.execute(delete(ChunkEmbedding))
    for chunk, vector in zip(chunks, vectors, strict=False):
        session.add(
            ChunkEmbedding(
                chunk_id=chunk.id,
                embedding=vector,
                model_name=settings.embedding_model_name,
                dim=settings.embedding_dim,
            )
        )
    session.commit()
    return len(chunks)
