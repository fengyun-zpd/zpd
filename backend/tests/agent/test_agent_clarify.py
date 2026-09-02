"""收口测试：补货助手澄清文案与预算处理（需求 3 / 说明书 §4.1）。

期望：
- 缺参澄清使用自然业务语言，不出现 warehouse_id / products / requested_window 等内部字段名；
- 澄清明确引导用户提供：仓库、SKU/商品分类、规划周期（7/14/30 天）；
- 用户提到预算时说明“预算暂不参与 V1 计算”，且预算绝不被当作必填参数；
- 预算输入不改变 V1 计算参数（仓库/SKU/窗口完全一致）。
"""

from __future__ import annotations

import pytest

from app.agent import offline
from app.agent.graph import node_clarify, node_classify

INTERNAL_FIELD_NAMES = ("warehouse_id", "products", "requested_window", "budget")


@pytest.mark.db
def test_missing_params_clarification_is_business_language(db_session):
    parsed = offline.parse_params("帮我补货", db_session)
    assert set(parsed.missing) == {"warehouse", "product", "requested_window"}
    assert parsed.clarification is not None
    # 不出现内部字段名
    for internal in INTERNAL_FIELD_NAMES:
        assert internal not in parsed.clarification
    # 业务引导明确
    assert "仓库" in parsed.clarification
    assert "SKU" in parsed.clarification
    assert "7/14/30" in parsed.clarification or "7、14、30" in parsed.clarification


def test_node_clarify_response_guides_without_internal_names():
    result = node_clarify({"missing_params": ["warehouse", "product", "requested_window"]})
    text = result["response"]
    for internal in INTERNAL_FIELD_NAMES:
        assert internal not in text
    assert "仓库" in text
    assert "SKU" in text
    assert "7/14/30" in text


@pytest.mark.db
def test_budget_never_becomes_required_param(db_session):
    parsed = offline.parse_params("帮我补货华东仓 SKU-E01 未来7天 预算 500 元", db_session)
    assert parsed.intent == "replenish"
    # 预算不进入计算参数，也不进入 missing
    assert "budget" not in parsed.params
    assert parsed.missing == []
    assert parsed.budget_note is True
    assert parsed.clarification is None  # 参数齐全，无需澄清


@pytest.mark.db
def test_budget_note_appears_in_clarification_when_params_missing(db_session):
    parsed = offline.parse_params("帮我补货，预算控制在 2000 元内", db_session)
    assert parsed.intent == "replenish"
    assert parsed.missing  # 仓库/SKU/窗口仍缺失，需要澄清
    assert parsed.budget_note is True
    assert parsed.clarification is not None
    assert "预算" in parsed.clarification
    assert "暂不参与" in parsed.clarification or "不参与" in parsed.clarification


@pytest.mark.db
def test_budget_input_does_not_change_v1_calculation_params(db_session):
    """预算不影响 V1 计算：带预算与不带预算解析出的计算参数完全一致。"""
    without = offline.parse_params("帮我检查华东仓未来两周需要补货的紧固件", db_session)
    with_budget = offline.parse_params("帮我检查华东仓未来两周需要补货的紧固件，预算不要超过 5000 元", db_session)
    assert with_budget.params == without.params
    assert with_budget.missing == without.missing
    assert with_budget.budget_note is True
    assert without.budget_note is False


def test_node_classify_normalizes_missing_to_whitelist(monkeypatch):
    """LLM/离线解析返回的 missing 只保留白名单业务参数，其余（如 budget）被丢弃。"""
    from sqlalchemy.orm import Session

    fake = offline.ParsedIntent(
        intent="replenish",
        params={"warehouse_id": "WH-E"},
        missing=["warehouse", "budget", "custom_field"],
        clarification=None,
        budget_note=True,
    )

    def _fake_parse(_text, _session):
        return fake  # noqa: ARG005 解析桩：固定返回，不访问 session

    monkeypatch.setattr(offline, "parse_params", _fake_parse)

    class _FakeFactory:
        def __enter__(self):
            return Session()  # fake parse 不查询，不会被使用

        def __exit__(self, *args):
            return False

    import app.agent.graph as graph_mod
    import app.db as db_mod

    def _fake_maker():
        return _FakeFactory()  # fake parse 不查询，不会被使用

    # node_classify 在函数内执行 `from app.db import get_session_factory`，
    # 因此需替换 app.db 模块上的绑定；返回可调用工厂（类似 sessionmaker）
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: _fake_maker)
    monkeypatch.setattr(graph_mod, "_llm_enabled", lambda: False)
    state = node_classify({"user_input": "帮我补货"})
    assert state["missing_params"] == ["warehouse"]  # budget/custom_field 被丢弃
    assert state["budget_note"] is True
