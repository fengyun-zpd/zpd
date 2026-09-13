"""Agent 决策链语义追踪测试（第六部分）。

无凭证时 Langfuse 是安全 no-op；这里注入 fake trace 捕获 span，固定"每轮 trace 至少
覆盖节点执行顺序、工具调用摘要与 RAG 引用"的行为，并断言不记录密钥 / 完整结果。
"""

from __future__ import annotations

import pytest

from app import observability
from app.agent.graph import run_turn


class _CapturedSpan(dict):
    """捕获 span 的 kwargs（name / input / output / metadata / level）。"""

    def end(self) -> None:
        pass


class _FakeTrace:
    def __init__(self, sink: list[dict]):
        self._sink = sink

    def span(self, **kwargs):
        self._sink.append(kwargs)
        return _CapturedSpan(kwargs)

    def generation(self, **kwargs):
        self._sink.append(kwargs)
        return _CapturedSpan(kwargs)

    def update(self, **kwargs) -> None:
        pass


class _TraceVar:
    """替代 ``observability._current_trace``：``get()`` 返回 fake trace。"""

    def __init__(self, trace) -> None:
        self._trace = trace

    def get(self, _default=None):
        return self._trace

    def set(self, value) -> None:  # pragma: no cover 兼容 turn_trace 的清理逻辑
        self._trace = value


@pytest.fixture()
def captured_spans(monkeypatch) -> list[dict]:
    sink: list[dict] = []
    monkeypatch.setattr(observability, "_current_trace", _TraceVar(_FakeTrace(sink)))
    return sink


@pytest.mark.db
def test_semantic_trace_covers_nodes_tools_and_citations(db_session, captured_spans):
    """每轮 trace 必须能回放：节点顺序、工具调用（摘要/规模）、RAG 引用。"""
    run_turn("t-trace-1", "alice", "帮我检查华东仓未来两周需要补货的紧固件")

    node_spans = [s for s in captured_spans if s.get("name") == "agent.node"]
    nodes = [s["metadata"]["node"] for s in node_spans]
    assert nodes[:3] == ["classify", "gather_evidence", "draft"], f"节点执行顺序应可回放，实际 {nodes}"

    tool_spans = [s for s in captured_spans if s.get("name") == "agent.tool"]
    assert tool_spans, "必须记录工具调用 span"
    tools_used = {s["metadata"]["tool"] for s in tool_spans}
    assert {
        "get_inventory",
        "get_supplier_options",
        "get_demand_history",
        "search_rules",
        "generate_draft",
    } <= tools_used
    for span in tool_spans:
        assert isinstance(span["input"]["args"], dict), "工具 span 必须带参数摘要"
        assert "result_size" in span["metadata"], "工具 span 必须带结果规模"

    rag_spans = [s for s in captured_spans if s.get("name") == "rag.search_rules"]
    assert rag_spans, "必须记录 RAG 检索 span"
    rag_meta = rag_spans[0]["metadata"]
    assert isinstance(rag_meta["cited_source_chunk_ids"], list)
    assert isinstance(rag_meta["cited_document_ids"], list)
    assert rag_meta["has_evidence"] is True, "有命中时必须标记有证据"


@pytest.mark.db
def test_turn_summary_trace_has_gate_and_degradation_fields(db_session, captured_spans):
    """轮次汇总 span 必须带降级原因、步数与门禁状态。"""
    run_turn("t-trace-2", "alice", "帮我补货")

    summary = next((s for s in captured_spans if s.get("name") == "agent.turn.summary"), None)
    assert summary is not None, "必须记录轮次汇总 span"
    meta = summary["metadata"]
    for field in (
        "intent",
        "plan_id",
        "interrupted",
        "offline",
        "degradation_reason",
        "step_count",
        "loop_blocked",
        "tool_count",
    ):
        assert field in meta, f"轮次汇总缺少字段：{field}"
    assert meta["step_count"] >= 1


@pytest.mark.db
def test_semantic_trace_contains_no_secrets_or_full_prompt(db_session, captured_spans):
    """语义追踪不得写入密钥、完整 Prompt 或完整检索正文。"""
    run_turn("t-trace-3", "alice", "帮我检查华东仓未来两周需要补货的紧固件")

    rendered = str(captured_spans)
    for forbidden in ("sk-", "api_key", "API_KEY", "secret"):
        assert forbidden not in rendered, f"trace 不得包含敏感内容：{forbidden}"
