"""管理员导入 RAG 资料：权限、幂等、检索与计算边界。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.errors import ForbiddenError
from app.models.knowledge import Chunk, Document
from app.models.replenishment import ReplenishmentRule
from app.rag.fts import keyword_search
from app.services.knowledge_import_service import MAX_CHUNK_CHARS, import_evidence_document


@pytest.mark.db
def test_admin_imports_evidence_as_searchable_chunks_without_changing_rules(db_session):
    before_rule_count = db_session.scalar(select(func.count()).select_from(ReplenishmentRule))
    content = (
        """# 关键客户到货异常处理

当供应商明确延期时，仓库应记录异常、复核在途数量，并由审批人决定是否创建替代补货建议。

"""
        + "补货证据资料。" * 220
    )

    result = import_evidence_document(
        db_session,
        actor_id="alice",
        idempotency_key="knowledge-import-idem-1",
        title="关键客户到货异常处理",
        doc_type="supplier_constraint",
        content=content,
        effective_from=date(2026, 9, 2),
        effective_to=None,
        request_id="req-knowledge-import-1",
    )

    assert result["already_exists"] is False
    assert result["chunk_count"] >= 2
    assert result["embedding_status"] in {"keyword_only", "vector_indexed"}
    chunks = db_session.scalars(
        select(Chunk).where(Chunk.document_id == result["document_id"]).order_by(Chunk.seq_no)
    ).all()
    assert len(chunks) == result["chunk_count"]
    assert all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in chunks)
    assert any(chunk.search_text for chunk in chunks)
    hits = keyword_search(db_session, "供应商 延期 异常", top_k=6)
    assert any(chunk.document_id == result["document_id"] for chunk, _ in hits)
    assert db_session.scalar(select(func.count()).select_from(ReplenishmentRule)) == before_rule_count


@pytest.mark.db
def test_knowledge_import_is_idempotent_and_requires_admin(db_session):
    payload = {
        "actor_id": "alice",
        "idempotency_key": "knowledge-import-idem-2",
        "title": "收货复核备忘",
        "doc_type": "receiving_sop",
        "content": "收货必须使用唯一 receipt_event_id，重复提交不得重复增加库存。",
        "effective_from": date(2026, 9, 2),
        "effective_to": None,
    }
    first = import_evidence_document(db_session, **payload)
    replay = import_evidence_document(db_session, **payload)
    assert replay == first
    assert db_session.scalar(select(func.count()).select_from(Document).where(Document.id == first["document_id"])) == 1

    with pytest.raises(ForbiddenError):
        import_evidence_document(
            db_session,
            actor_id="bob",
            idempotency_key="knowledge-import-idem-operator",
            title="操作员无权导入",
            doc_type="receiving_sop",
            content="这条资料不应被写入知识库。",
            effective_from=date(2026, 9, 2),
            effective_to=None,
        )
