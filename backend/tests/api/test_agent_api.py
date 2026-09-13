"""HTTP Agent 主链路测试（db）：start / resume / state 与安全边界。

边界：身份事实源是 ``X-Actor-Id``；resume 只读已提交决定、不产生业务副作用；
state 只返回安全摘要（不含密钥 / 完整 Prompt / 检索正文）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

_ALICE = {"X-Actor-Id": "alice"}
_BOB = {"X-Actor-Id": "bob"}


@pytest.fixture()
def client(db_session) -> TestClient:
    return TestClient(create_app())


def _sse_events(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析为 (event, data) 列表。"""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data: str | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event and data is not None:
            events.append((event, json.loads(data)))
    return events


def _start(client: TestClient, content: str = "帮我补货") -> tuple[str, list[tuple[str, dict]]]:
    resp = client.post("/api/v1/agent/start", json={"content": content}, headers=_ALICE)
    assert resp.status_code == 200, resp.text
    thread_id = resp.headers.get("x-thread-id")
    assert thread_id, "响应头必须包含 X-Thread-Id"
    return thread_id, _sse_events(resp.text)


@pytest.mark.db
def test_agent_start_streams_events_and_headers(client):
    """start：SSE 逐节点事件 + X-Request-Id / X-Thread-Id 响应头。"""
    thread_id, events = _start(client)
    assert thread_id
    names = [event for event, _ in events]
    assert names[0] == "agent_start"
    assert "node_end" in names
    assert "message" in names
    assert names[-1] == "done"

    start_event = events[0][1]
    assert start_event["thread_id"] == thread_id
    assert "degradation_reason" in start_event

    message = next(data for event, data in events if event == "message")
    assert message.get("step_count")
    assert "loop_blocked" in message


@pytest.mark.db
def test_agent_state_returns_safe_summary(client):
    """state：返回安全摘要，且不含密钥 / Prompt 等敏感内容。"""
    thread_id, _ = _start(client)
    resp = client.get(f"/api/v1/agent/{thread_id}/state", headers=_ALICE)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["thread_id"] == thread_id
    assert data["intent"] == "replenish"
    assert data["missing_params"], "缺参请求应记录缺失字段"
    assert data["offline"] is True
    assert data["degradation_reason"]
    assert isinstance(data["step_count"], int)
    for field in ("outcome", "plan_id", "needs_approval", "response", "loop_blocked"):
        assert field in data

    lowered = resp.text.lower()
    for forbidden in ("api_key", "sk-", "secret", "prompt"):
        assert forbidden not in lowered


@pytest.mark.db
def test_agent_state_rejects_other_actor(client):
    """跨演示用户读取会话状态必须被拒绝。"""
    thread_id, _ = _start(client)
    resp = client.get(f"/api/v1/agent/{thread_id}/state", headers=_BOB)
    assert resp.status_code == 403


@pytest.mark.db
def test_agent_resume_with_content_continues_same_thread(client):
    """澄清补充：继续同一会话（不新建 thread），并走同样的 SSE 路径。"""
    thread_id, _ = _start(client)
    resp = client.post(
        f"/api/v1/agent/{thread_id}/resume",
        json={"content": "华东仓，紧固件，14天"},
        headers=_ALICE,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-thread-id") == thread_id
    events = _sse_events(resp.text)
    assert [event for event, _ in events][0] == "agent_start"
    assert [event for event, _ in events][-1] == "done"


@pytest.mark.db
def test_agent_resume_rejects_empty_request(client):
    """resume 必须二选一：content 或 plan_id + decision_version。"""
    thread_id, _ = _start(client)
    resp = client.post(f"/api/v1/agent/{thread_id}/resume", json={}, headers=_ALICE)
    assert resp.status_code == 422


@pytest.mark.db
def test_agent_resume_rejects_mixed_request(client):
    """content 与审批恢复参数不可同时提供。"""
    thread_id, _ = _start(client)
    resp = client.post(
        f"/api/v1/agent/{thread_id}/resume",
        json={"content": "补充", "plan_id": "plan-x", "decision_version": 1},
        headers=_ALICE,
    )
    assert resp.status_code == 422


@pytest.mark.db
def test_agent_resume_plan_not_found(client):
    """审批恢复：计划不存在返回稳定 404。"""
    thread_id, _ = _start(client)
    resp = client.post(
        f"/api/v1/agent/{thread_id}/resume",
        json={"plan_id": "plan-does-not-exist", "decision_version": 1},
        headers=_ALICE,
    )
    assert resp.status_code == 404


@pytest.mark.db
def test_agent_resume_plan_version_mismatch(client):
    """审批恢复：决定版本不匹配返回稳定 409（不执行恢复）。"""
    thread_id, _ = _start(client, "帮我检查华东仓未来两周需要补货的紧固件")
    state = client.get(f"/api/v1/agent/{thread_id}/state", headers=_ALICE).json()["data"]
    plan_id = state.get("plan_id")
    if not plan_id:  # 库内已有活动建议时可能阻断，此时跳过版本断言
        pytest.skip("本次未生成计划（可能被活动建议阻断），跳过版本不匹配用例")

    resp = client.post(
        f"/api/v1/agent/{thread_id}/resume",
        json={"plan_id": plan_id, "decision_version": 999},
        headers=_ALICE,
    )
    assert resp.status_code == 409


@pytest.mark.db
def test_agent_resume_rejects_other_actor(client):
    """跨演示用户 resume 必须被拒绝。"""
    thread_id, _ = _start(client)
    resp = client.post(
        f"/api/v1/agent/{thread_id}/resume",
        json={"content": "华东仓"},
        headers=_BOB,
    )
    assert resp.status_code == 403


@pytest.mark.db
def test_agent_resume_rejects_invalid_last_event_id(client):
    """非法 Last-Event-ID 必须返回稳定 422，不得静默开启新一轮任务。"""
    thread_id, _ = _start(client)
    before = client.get(f"/api/v1/agent/{thread_id}/state", headers=_ALICE).json()["data"]

    for bad in ("not-a-cursor", "abc:-1", ":7", "abc:"):
        resp = client.post(
            f"/api/v1/agent/{thread_id}/resume",
            json={"content": "华东仓"},
            headers={**_ALICE, "Last-Event-ID": bad},
        )
        assert resp.status_code == 422, f"{bad!r} 应被拒绝，实际 {resp.status_code}"

    # 状态不变：不得因为非法游标而执行了新的一轮
    after = client.get(f"/api/v1/agent/{thread_id}/state", headers=_ALICE).json()["data"]
    assert after.get("step_count") == before.get("step_count"), "非法游标不得触发新的 Agent 轮次"


@pytest.mark.db
def test_agent_start_rejects_invalid_last_event_id(client):
    """start 收到非法游标同样返回稳定 422（不创建议会）。"""
    resp = client.post(
        "/api/v1/agent/start",
        json={"content": "帮我补货"},
        headers={**_ALICE, "Last-Event-ID": "garbage"},
    )
    assert resp.status_code == 422


@pytest.mark.db
def test_agent_resume_rejects_plan_from_other_thread(client, db_session, monkeypatch):
    """同一 actor 用**其他会话**的 plan_id 恢复：稳定 403，不触发 LangGraph resume。

    并断言没有产生业务副作用（计划状态不变、不新建采购单）。
    """
    from sqlalchemy import select

    from app.agent import graph as graph_module
    from app.models.purchasing import PurchaseOrder
    from app.models.replenishment import ReplenishmentPlan

    # 会话 A：生成草稿并挂起（产生 plan，绑定到 thread_a）
    thread_a, _ = _start(client, "帮我检查华东仓未来两周需要补货的紧固件")
    state = client.get(f"/api/v1/agent/{thread_a}/state", headers=_ALICE).json()["data"]
    plan_id = state.get("plan_id")
    if not plan_id:
        pytest.skip("本次未生成计划（可能被活动建议阻断），跳过跨会话恢复用例")

    # 会话 B：同一 actor 的另一个合法会话
    thread_b, _ = _start(client, "帮我补货")

    # 监控是否真的进入 LangGraph 恢复（必须为 0）
    resume_calls = {"count": 0}
    original_resume = graph_module.astream_resume

    def _spy(*args, **kwargs):
        resume_calls["count"] += 1
        return original_resume(*args, **kwargs)

    monkeypatch.setattr(graph_module, "astream_resume", _spy)

    before_plan = db_session.get(ReplenishmentPlan, plan_id)
    before_status = before_plan.status
    before_pos = db_session.scalars(select(PurchaseOrder)).all()

    resp = client.post(
        f"/api/v1/agent/{thread_b}/resume",
        json={"plan_id": plan_id, "decision_version": int(before_plan.version)},
        headers=_ALICE,
    )

    assert resp.status_code == 403, resp.text
    assert resume_calls["count"] == 0, "跨会话恢复不得进入 LangGraph resume"

    db_session.expire_all()
    after_plan = db_session.get(ReplenishmentPlan, plan_id)
    assert after_plan.status == before_status, "失败路径不得改变计划状态"
    assert len(db_session.scalars(select(PurchaseOrder)).all()) == len(before_pos), "不得新建采购单"


@pytest.mark.db
def test_checkpoint_awaiting_resume_semantics(client):
    """checkpoint 等待恢复判定：未跑过/已结束为 False，草稿挂起且 plan_id 一致为 True。"""
    from app.api.agent import _checkpoint_awaiting_resume

    assert _checkpoint_awaiting_resume("thread-never-run", "any-plan") is False

    # 澄清路径：图走到 END，无待执行节点
    clarify_thread, _ = _start(client, "帮我补货")
    assert _checkpoint_awaiting_resume(clarify_thread, "any-plan") is False

    # 草稿路径：wait 节点 interrupt 挂起
    draft_thread, _ = _start(client, "帮我检查华东仓未来两周需要补货的紧固件")
    state = client.get(f"/api/v1/agent/{draft_thread}/state", headers=_ALICE).json()["data"]
    plan_id = state.get("plan_id")
    if not plan_id:
        pytest.skip("本次未生成计划（可能被活动建议阻断）")
    assert _checkpoint_awaiting_resume(draft_thread, plan_id) is True
    assert _checkpoint_awaiting_resume(draft_thread, "other-plan") is False


@pytest.mark.db
def test_agent_resume_succeeds_with_matching_thread_plan_version(client, db_session):
    """只有 thread + plan + version 全部匹配才允许恢复；且只读决定、不执行审批。"""
    from sqlalchemy import select

    from app.models.replenishment import PlanLine, ReplenishmentPlan
    from app.services import plan_service

    thread_id, _ = _start(client, "帮我检查华东仓未来两周需要补货的紧固件")
    state = client.get(f"/api/v1/agent/{thread_id}/state", headers=_ALICE).json()["data"]
    plan_id = state.get("plan_id")
    if not plan_id:
        pytest.skip("本次未生成计划（可能被活动建议阻断），跳过恢复成功用例")

    # 审批由 REST/领域服务完成（先落库），resume 只读取结果
    line = db_session.scalar(select(PlanLine).where(PlanLine.plan_id == plan_id))
    plan_service.decide_plan(
        db_session,
        plan_id=plan_id,
        actor_id="carol",
        mode="approve",
        decisions={line.id: "approve"},
        idempotency_key="agent-api-resume-ok-1",
    )
    db_session.commit()
    expected_version = int(db_session.get(ReplenishmentPlan, plan_id).version)

    resp = client.post(
        f"/api/v1/agent/{thread_id}/resume",
        json={"plan_id": plan_id, "decision_version": expected_version},
        headers=_ALICE,
    )
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    assert [event for event, _ in events][0] == "agent_start"
    assert [event for event, _ in events][-1] == "done"
    message = next(data for event, data in events if event == "message")
    assert "审批通过" in message["content"]
