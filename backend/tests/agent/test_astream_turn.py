"""真流式 astream_turn 事件流测试（需求 8.2 增强）。

验证：逐节点产出进度事件（agent_start / node_end / tool_call / draft_created /
interrupted / message / done），且最终 message/done 与 run_turn 语义一致。
"""

from __future__ import annotations

import pytest

from app.agent.graph import astream_turn


async def _collect(thread_id: str, text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async for event, data in astream_turn(thread_id, "alice", text):
        events.append((event, data))
    return events


@pytest.mark.db
async def test_astream_clarify_events(db_session):
    events = await _collect("t-stream-1", "帮我补货")
    names = [e for e, _ in events]
    assert names[0] == "agent_start"
    assert "node_end" in names
    assert names[-1] == "done"
    msg = next(d for e, d in events if e == "message")
    assert "还需要补充" in msg["content"]
    assert msg["outcome"] == "clarified"
    nodes = [d["node"] for e, d in events if e == "node_end"]
    assert "classify" in nodes and "clarify" in nodes


@pytest.mark.db
async def test_astream_full_turn_events(db_session):
    events = await _collect("t-stream-2", "帮我检查华东仓未来两周需要补货的紧固件")
    names = [e for e, _ in events]
    assert "draft_created" in names
    assert "interrupted" in names
    assert "tool_call" in names  # 只读工具 + 草稿工具逐条推送
    done = next(d for e, d in events if e == "done")
    assert done["interrupted"] is True
    assert done.get("plan_id")
    msg = next(d for e, d in events if e == "message")
    assert "已生成补货草稿" in msg["content"]
