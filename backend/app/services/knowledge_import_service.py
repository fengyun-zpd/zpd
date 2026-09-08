"""管理员本地知识资料导入（仅作 RAG 证据，不直接修改业务规则）。"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import ValidationError
from app.models.knowledge import Chunk, ChunkEmbedding, Document
from app.rag.fts import to_search_text
from app.services.access import require_roles
from app.services.audit import write_audit
from app.services.hashing import payload_hash
from app.services.idempotency import IdempotencyGuard, replay_failure

ALLOWED_DOCUMENT_TYPES = frozenset(
    {
        "replenishment_policy",
        "safety_stock",
        "warehouse_rule",
        "supplier_constraint",
        "receiving_sop",
    }
)
MAX_CONTENT_CHARS = 80_000
MAX_CHUNK_CHARS = 1_200
IMPORT_COMMAND = "import_knowledge_evidence"
IMPORT_AGGREGATE = "knowledge_document_import"


def _normalise_content(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def split_evidence_chunks(content: str) -> list[str]:
    """按段落切分，过长段落再安全拆分，保证每块均可独立检索。"""
    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""

    def append_current() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for paragraph in paragraphs:
        if len(paragraph) > MAX_CHUNK_CHARS:
            append_current()
            remaining = paragraph
            while len(remaining) > MAX_CHUNK_CHARS:
                boundary = remaining.rfind("\n", 0, MAX_CHUNK_CHARS)
                if boundary < MAX_CHUNK_CHARS // 2:
                    boundary = MAX_CHUNK_CHARS
                chunks.append(remaining[:boundary].strip())
                remaining = remaining[boundary:].lstrip()
            if remaining:
                current = remaining
            continue

        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) > MAX_CHUNK_CHARS:
            append_current()
            current = paragraph
        else:
            current = candidate
    append_current()
    return chunks


def import_evidence_document(
    session: Session,
    *,
    actor_id: str,
    idempotency_key: str,
    title: str,
    doc_type: str,
    content: str,
    effective_from: date,
    effective_to: date | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """导入一篇可检索资料，永不从自然语言生成/修改结构化计算规则。"""
    require_roles(session, actor_id, "admin")
    normalized_title = title.strip()
    normalized_content = _normalise_content(content)
    if doc_type not in ALLOWED_DOCUMENT_TYPES:
        raise ValidationError("不支持的知识资料类型")
    if not normalized_title:
        raise ValidationError("资料标题不能为空")
    if not normalized_content:
        raise ValidationError("资料内容不能为空")
    if len(normalized_content) > MAX_CONTENT_CHARS:
        raise ValidationError(f"资料内容不能超过 {MAX_CONTENT_CHARS} 个字符")
    if effective_to is not None and effective_to < effective_from:
        raise ValidationError("失效日期不能早于生效日期")

    payload = {
        "title": normalized_title,
        "doc_type": doc_type,
        "content": normalized_content,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }
    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=IMPORT_COMMAND,
        aggregate_ref=IMPORT_AGGREGATE,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    begun = guard.begin()
    if begun.action == "replay_success":
        return begun.response
    if begun.action == "replay_failure":
        replay_failure(begun)

    content_digest = payload_hash(payload)
    document_id = f"usr-doc-{content_digest[:40]}"
    existing = session.get(Document, document_id)
    if existing is not None:
        response = {
            "document_id": existing.id,
            "title": existing.title,
            "chunk_count": session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == existing.id)
            )
            or 0,
            "embedding_status": "already_indexed",
            "already_exists": True,
        }
        guard.succeed(response, entity_type="document", entity_id=existing.id)
        return response

    document = Document(
        id=document_id,
        title=normalized_title,
        doc_type=doc_type,
        version=1,
        effective_from=effective_from,
        effective_to=effective_to,
        enabled=True,
        content_text=normalized_content,
    )
    chunks = split_evidence_chunks(normalized_content)
    session.add(document)
    # Document has no ORM relationship to Chunk; persist the parent first so
    # PostgreSQL can satisfy the foreign key when chunks are flushed.
    session.flush()
    chunk_models: list[Chunk] = []
    for sequence, chunk_content in enumerate(chunks, start=1):
        chunk = Chunk(
            id=f"{document_id}-c{sequence}",
            document_id=document_id,
            seq_no=sequence,
            content=chunk_content,
            search_text=to_search_text(chunk_content),
        )
        session.add(chunk)
        chunk_models.append(chunk)
    session.flush()

    embedding_status = "keyword_only"
    try:
        from app.rag.embedding import embed_texts

        vectors = embed_texts([chunk.content for chunk in chunk_models])
        settings = get_settings()
        for chunk, vector in zip(chunk_models, vectors, strict=True):
            session.add(
                ChunkEmbedding(
                    chunk_id=chunk.id,
                    embedding=vector,
                    model_name=settings.embedding_model_name,
                    dim=settings.embedding_dim,
                )
            )
        embedding_status = "vector_indexed"
    except Exception:  # Embedding 是可选增强；关键词检索仍须可用。
        embedding_status = "keyword_only"

    response = {
        "document_id": document.id,
        "title": document.title,
        "chunk_count": len(chunk_models),
        "embedding_status": embedding_status,
        "already_exists": False,
    }
    write_audit(
        session,
        actor_id=actor_id,
        action="import_knowledge_evidence",
        entity_type="document",
        entity_id=document.id,
        after_state={
            "title": document.title,
            "doc_type": document.doc_type,
            "content_digest": content_digest,
            "chunk_count": len(chunk_models),
            "embedding_status": embedding_status,
            "calculation_rule_changed": False,
        },
        request_id=request_id,
        idempotency_operation_id=guard.operation.id if guard.operation else None,
    )
    guard.succeed(response, entity_type="document", entity_id=document.id)
    return response
