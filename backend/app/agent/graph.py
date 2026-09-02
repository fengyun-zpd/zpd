"""LangGraph 图：补货助手（需求 3 / 架构 3）。

classify -> clarify | gather_evidence -> draft(interrupt) -> finalize
状态图与数据库权威：checkpoint 只保存会话恢复状态；业务状态以数据库为准。
"""

from __future__ import annotations

import logging
from contextlib import suppress

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from app.agent import offline, tools
from app.agent.state import AgentState
from app.config import get_settings
from app.errors import StockMindError

logger = logging.getLogger("stockmind.agent.graph")


def _llm_enabled() -> bool:
    return bool(get_settings().llm_api_key)


# ---------------------------------------------------------------- 节点


def node_classify(state: AgentState) -> dict:
    text = state.get("user_input", "")
    from app.db import get_session_factory

    factory = get_session_factory()
    result: dict = {
        "intent": "other",
        "params": {},
        "missing_params": [],
        "clarification": None,
        "offline": not _llm_enabled(),
    }
    with factory() as session:
        parsed = offline.parse_params(text, session)
        result.update(
            {
                "intent": parsed.intent,
                "params": parsed.params,
                "missing_params": parsed.missing,
                "clarification": parsed.clarification,
            }
        )
        if _llm_enabled():
            try:
                from app.agent.llm import llm_parse_params

                llm_result = llm_parse_params(text)
                result["intent"] = llm_result.get("intent", parsed.intent)
                # 归一化商品范围：分类名展开为具体 SKU（LLM 返回"紧固件"等分类时）
                llm_products = list(llm_result.get("params", {}).get("products", []) or [])
                expanded = _expand_products(session, llm_products)
                params = dict(llm_result.get("params", {}))
                params["products"] = expanded if expanded else llm_products
                result["params"] = params
                missing = list(llm_result.get("missing", parsed.missing))
                if not params.get("products"):
                    missing = list(dict.fromkeys(missing + ["product"]))
                result["missing_params"] = missing
                result["clarification"] = llm_result.get("clarification")
                result["offline"] = False
            except Exception as exc:  # noqa: BLE001 LLM 不可用时回退离线
                logger.warning("LLM 解析失败，回退离线模式: %s", exc)
                result["offline"] = True
    return result


def _expand_products(session, products: list[str]) -> list[str]:
    """把分类名/商品名展开为具体 SKU；SKU id 原样保留。"""
    from app.models.inventory import Product

    out: list[str] = []
    for item in products:
        if not item:
            continue
        if item.startswith("SKU-"):
            out.append(item)
            continue
        rows = session.scalars(select(Product).where((Product.category == item) | (Product.name == item))).all()
        if rows:
            out.extend(r.id for r in rows)
        else:
            out.append(item)  # 未知值保留，由后续领域校验决定
    return list(dict.fromkeys(out))


def route_after_classify(state: AgentState) -> str:
    intent = state.get("intent", "other")
    if intent != "replenish":
        return "respond"
    if state.get("missing_params"):
        return "clarify"
    return "gather_evidence"


def node_clarify(state: AgentState) -> dict:
    missing = list(state.get("missing_params", []) or [])
    label = {
        "warehouse": "仓库",
        "product": "SKU 或商品范围",
        "requested_window": "规划窗口（7/14/30 天）",
    }
    missing_label = "、".join(label.get(m, m) for m in missing) or "仓库、SKU、规划窗口"
    return {
        "response": f"缺少必要参数：{missing_label}。请补充后重试。",
        "outcome": "clarified",
        "missing_params": missing,
    }


def node_gather_evidence(state: AgentState) -> dict:
    """只读工具编排：库存、在途、供应商关系、规则候选（证据，不进计算）。"""
    from app.observability import record_span

    params = state.get("params", {})
    warehouse_id = params.get("warehouse_id")
    products = list(params.get("products", []) or [])
    calls = list(state.get("tool_calls", []))
    for pid in products:
        assert isinstance(warehouse_id, str)  # 路由已保证必要参数完整
        tools.record(
            calls,
            "get_inventory",
            {"warehouse_id": warehouse_id, "product_id": pid},
            tools.get_inventory(warehouse_id, pid),
        )
        tools.record(calls, "get_supplier_options", {"product_id": pid}, tools.get_supplier_options(pid))
        tools.record(
            calls,
            "get_demand_history",
            {"warehouse_id": warehouse_id, "product_id": pid},
            tools.get_demand_history(warehouse_id, pid),
        )
    evidence = tools.search_rules(f"安全库存 补货 规则 {warehouse_id}")
    tools.record(calls, "search_rules", {"query": "安全库存 补货 规则"}, evidence)
    record_span(
        name="rag.search_rules",
        input_={"query": "安全库存 补货 规则", "warehouse_id": warehouse_id},
        output={"hit_count": len(evidence.get("hits", []))},
        metadata={"products": products},
    )
    return {"tool_calls": calls, "rule_evidence": evidence.get("hits", [])}


def node_draft(state: AgentState) -> dict:
    """受控草稿工具：领域服务校验并落库；提交待审批后 interrupt 暂停会话。"""
    from app.agent.blocked_hints import blocked_line_summary
    from app.observability import record_span

    params = state.get("params", {})
    calls = list(state.get("tool_calls", []))
    try:
        draft = tools.generate_draft(
            actor_id=state.get("actor_id", "operator"),
            warehouse_id=params["warehouse_id"],
            products=params["products"],
            requested_window=params["requested_window"],
            thread_id=state.get("thread_id"),
        )
    except StockMindError as exc:
        tools.record(calls, "generate_draft", params, None, exc.message)
        record_span(
            name="domain.generate_draft",
            input_=params,
            output=None,
            metadata={"error": exc.code, "message": exc.message[:300]},
            level="ERROR",
        )
        # 防重/并发类错误给出明确下一步；其余阻断按错误码提示
        hint = blocked_line_summary("", exc.code, exc.message)
        return {
            "tool_calls": calls,
            "draft_result": None,
            "outcome": "blocked",
            "blocked_lines": [hint],
            "response": f"无法生成草稿：{exc.message}。下一步：{hint['next_step']}",
        }
    tools.record(calls, "generate_draft", params, draft)
    record_span(
        name="domain.generate_draft",
        input_=params,
        output=draft,
        metadata={"plan_id": draft.get("plan_id"), "created": draft.get("created")},
    )
    if not draft.get("created"):
        lines = draft.get("lines", [])
        blocked = [o for o in lines if o.get("flag") == "blocked"]
        summaries = [
            blocked_line_summary(o.get("product_id", ""), o.get("blocked_code"), o.get("blocked_reason"))
            for o in blocked
        ]
        reason = "；".join(f"{o['product_id']}: {o['blocked_reason']}" for o in blocked) if blocked else "无有效明细"
        return {
            "tool_calls": calls,
            "draft_result": draft,
            "outcome": "blocked",
            "blocked_lines": summaries,
            "response": f"本次没有可提交的补货建议。阻断原因：{reason}",
        }
    # 草稿已落库并提交待审批：先在状态中记录，再进入 wait 节点 interrupt 暂停
    return {
        "tool_calls": calls,
        "draft_result": draft,
        "plan_id": draft["plan_id"],
        "needs_approval": True,
        "outcome": "draft_created",
        "response": (f"已生成补货草稿（计划 {draft['plan_id']}）并提交待审批，会话已暂停；审批人处理后会话自动恢复。"),
    }


def node_wait(state: AgentState) -> dict:
    """审批等待点：LangGraph interrupt 暂停会话；resume 后返回审批结果引用。"""
    decision = interrupt(
        {
            "status": "pending_approval",
            "plan_id": state.get("plan_id"),
            "thread_id": state.get("thread_id"),
        }
    )
    # resume 后：decision 为已提交业务决定的引用（不执行审批，仅读取）
    return {"decision": decision if isinstance(decision, dict) else {}}


def node_finalize(state: AgentState) -> dict:
    """resume 后读取数据库中已提交的带版本号业务决定并解释（宪法第七条）。"""
    plan_id = state.get("plan_id")
    decision = state.get("decision") or {}
    status = decision.get("status")
    if status is None:
        # 第一次运行到 finalize（无 interrupt）的情况不应发生；防御性兜底
        assert plan_id is not None
        return {"response": offline.build_explanation(plan_id, state.get("draft_result")), "outcome": "answered"}
    label = {
        "approved": "审批通过",
        "rejected": "整单驳回",
        "superseded": "已被修订版替代",
    }.get(status, status)
    if status == "approved":
        return {
            "response": f"计划 {plan_id} 审批通过。可在采购单页面按供应商创建采购单并下达。",
            "outcome": "approved",
        }
    return {
        "response": f"计划 {plan_id} 的处理结果为：{label}。如需重新计算，请操作员生成修订版。",
        "outcome": "answered",
    }


def node_respond(state: AgentState) -> dict:
    intent = state.get("intent", "other")
    text = state.get("user_input", "")
    if intent == "query":
        return {"response": _query_response(text), "outcome": "answered"}
    if intent == "explain":
        return {
            "response": "补货建议依据《补货策略总则》《安全库存规则》等文档，按确定性公式计算；"
            "请查看计划明细中的公式、数据快照与规则引用（不可信检索文本不改变系统边界）。",
            "outcome": "answered",
        }
    return {
        "response": "我只能处理补货、库存查询与规则解释；该请求超出范围，无法执行。",
        "outcome": "answered",
    }


def _query_response(text: str) -> str:
    from app.agent import tools as _tools

    parts = []
    if "华东" in text or "WH-E" in text:
        pid = "SKU-E01"
        parts.append(str(_tools.get_inventory("WH-E", pid)))
    return "\n".join(parts) if parts else "请指定仓库与 SKU 后再查询（例如：华东仓 SKU-E01 库存）。"


# ---------------------------------------------------------------- 图构造


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("classify", node_classify)
    builder.add_node("clarify", node_clarify)
    builder.add_node("gather_evidence", node_gather_evidence)
    builder.add_node("draft", node_draft)
    builder.add_node("wait", node_wait)
    builder.add_node("finalize", node_finalize)
    builder.add_node("respond", node_respond)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        route_after_classify,
        {"clarify": "clarify", "gather_evidence": "gather_evidence", "respond": "respond"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("respond", END)
    builder.add_edge("gather_evidence", "draft")
    builder.add_edge("draft", "wait")
    builder.add_edge("wait", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=_build_checkpointer(_checkpoint_conn))


def _build_checkpointer(conn=None):
    """构造 LangGraph checkpoint（仅会话恢复状态；业务状态以数据库为准）。

    默认 PostgreSQL（compose/生产路径）；CHECKPOINTER_BACKEND=memory 用于
    开发/测试（测试会重建 schema，Postgres checkpoint 表随之重建，改用内存避免失效）。

    连接由调用方持有（_checkpoint_conn）并在失效时重建，避免长驻连接在
    DROP SCHEMA / 数据库重启后被终止而无法自愈。
    """
    if get_settings().checkpointer_backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        from app.config import get_settings as _settings

        if conn is None:
            dsn = _settings().postgres_dsn.replace("postgresql+psycopg://", "postgresql://")
            conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()
        return saver
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL checkpoint 不可用，回退内存 checkpoint: %s", exc)
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()


# 当前 graph 与底层 Postgres checkpoint 连接（连接失效时用于探测/重建）
_graph = None
_checkpoint_conn = None


def _checkpoint_healthy() -> bool:
    """检测 Postgres checkpoint 连接是否仍可用（内存后端恒为 True）。"""
    if get_settings().checkpointer_backend == "memory":
        return True
    if _checkpoint_conn is None:
        return False
    try:
        _checkpoint_conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 连接被终止/关闭
        return False


def _reset_graph() -> None:
    """失效 checkpoint 连接时重建 graph（自愈：不依赖容器重启）。"""
    global _graph, _checkpoint_conn
    try:
        if _checkpoint_conn is not None:
            with suppress(Exception):
                _checkpoint_conn.close()
            _checkpoint_conn = None
        _graph = None
        _ensure_graph()
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint 重建失败，将在下次调用时重试: %s", exc)


def _ensure_graph():
    global _graph, _checkpoint_conn
    if _graph is None:
        if get_settings().checkpointer_backend != "memory":
            try:
                import psycopg
                from psycopg.rows import dict_row

                from app.config import get_settings as _settings

                dsn = _settings().postgres_dsn.replace("postgresql+psycopg://", "postgresql://")
                conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
                _checkpoint_conn = conn
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkpoint 连接创建失败: %s", exc)
                _checkpoint_conn = None
        _graph = build_graph()
    return _graph


def get_graph():
    if _graph is not None and not _checkpoint_healthy():
        logger.warning("Postgres checkpoint 连接已失效，重建 graph")
        _reset_graph()
    return _ensure_graph()


def run_turn(thread_id: str, actor_id: str, user_input: str) -> tuple[dict, bool]:
    """执行一轮会话；返回 (state, interrupted)。

    interrupted=True 表示会话在草稿提交待审批处暂停（等待审批后 resume）。
    中断点位于独立的 wait 节点；invoke 在中断处返回时，draft 节点的状态
    （plan_id / needs_approval / response）已经写入，据此判定暂停。

    Langfuse 观测（可选）：无凭证时安全 no-op；观测失败不影响业务。
    """
    from app.observability import flush, mark_error, record_span, turn_trace

    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    initial: AgentState = {
        "thread_id": thread_id,
        "actor_id": actor_id,
        "user_input": user_input,
        "tool_calls": [],
        "rule_evidence": [],
    }
    with turn_trace(
        name="agent.turn",
        thread_id=thread_id,
        actor_id=actor_id,
        input_=user_input,
        metadata={"intent_source": "run_turn"},
        tags=["agent", "v1"],
    ):
        try:
            result = graph.invoke(initial, config=config)
        except GraphInterrupt as exc:  # pragma: no cover 版本兼容兜底
            result = {"needs_approval": True}
            interrupts = exc.args[0] if exc.args else []
            if interrupts:
                first = interrupts[0]
                payload = first.value if hasattr(first, "value") else first
                result["plan_id"] = (payload or {}).get("plan_id")
            record_span(name="agent.interrupt", metadata={"plan_id": result.get("plan_id")})
            return result, True
        except Exception as exc:  # noqa: BLE001 记录观测后继续抛出（不掩盖业务错误）
            mark_error(f"{type(exc).__name__}: {exc}")
            record_span(name="agent.error", metadata={"error": str(exc)[:500]}, level="ERROR")
            raise
        interrupted = bool(result.get("needs_approval"))
        record_span(
            name="agent.turn.summary",
            metadata={
                "intent": result.get("intent"),
                "plan_id": result.get("plan_id"),
                "interrupted": interrupted,
                "offline": result.get("offline"),
                "tool_count": len(result.get("tool_calls", [])),
            },
        )
        flush()
        return result, interrupted


def resume_workflow(thread_id: str, plan_id: str, decision_version: int) -> dict:
    """读取已提交的业务决定并恢复会话（只恢复对话流程，不执行审批）。"""
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        Command(
            resume={
                "status": _decision_status(plan_id),
                "plan_id": plan_id,
                "decision_version": decision_version,
            }
        ),
        config=config,
    )
    return result


def _decision_status(plan_id: str) -> str:
    from app.db import get_session_factory
    from app.models.replenishment import ReplenishmentPlan

    factory = get_session_factory()
    with factory() as session:
        plan = session.get(ReplenishmentPlan, plan_id)
        return plan.status if plan else "unknown"
