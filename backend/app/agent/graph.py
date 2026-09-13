"""LangGraph 图：补货助手（需求 3 / 架构 3）。

classify -> clarify | gather_evidence -> draft(interrupt) -> finalize
状态图与数据库权威：checkpoint 只保存会话恢复状态；业务状态以数据库为准。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from contextlib import suppress
from functools import wraps

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select

from app.agent import offline, tools
from app.agent.state import AgentState
from app.config import get_settings
from app.errors import StockMindError

logger = logging.getLogger("stockmind.agent.graph")


def _llm_mode() -> str:
    """LLM 运行模式：auto（默认）/ llm（评测真实路径）/ offline（强制离线）。"""
    return (get_settings().llm_mode or "auto").strip().lower()


def _llm_configured() -> bool:
    """真实 LLM 配置是否完整（Key / Base URL / Model 三者齐备）。"""
    s = get_settings()
    return bool(s.llm_api_key and s.llm_base_url and s.llm_model)


def _llm_enabled() -> bool:
    """是否应尝试调用真实 LLM。

    - ``offline``：从不调用外部模型（评测确定性基线，强制离线）；
    - ``llm``：评测真实模型路径，必须配置完整；配置不完整时由 ``node_classify``
      记录 ``LLM_NOT_CONFIGURED`` 并回退 OFFLINE，不发起请求也不假装调用成功；
    - ``auto``（默认）：配置完整才优先真实 LLM，失败后回退 OFFLINE 并记录原因。
    """
    if _llm_mode() == "offline":
        return False
    return _llm_configured()


# ---------------------------------------------------------------- 节点

# 步数/循环门禁触发时的稳定业务语言（此后不再调用任何工具）
ESCALATE_RESPONSE = "任务步骤超过安全上限，已停止自动处理，请转人工处理。"


def _canonical_args(args: dict) -> str:
    """工具参数规范化：key 升序 + 紧凑分隔符，保证同参数生成同一 key。"""
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _tool_key(tool_name: str, args: dict) -> str:
    """重复检测 key = 工具名 + 规范化参数（不同参数不会被误判为重复）。"""
    return f"{tool_name}|{_canonical_args(args)}"


def _tool_result_size(result: object) -> int | None:
    """工具结果规模（条目数 / 字段数），用于语义追踪，**不记录结果内容**。"""
    if result is None:
        return None
    if isinstance(result, dict):
        for key in ("warehouses", "products", "candidates", "days", "hits", "lines"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
        return len(result)
    if isinstance(result, list):
        return len(result)
    return None


_TOOL_ARG_SUMMARY_LIMIT = 40


def _tool_args_summary(args: dict) -> dict:
    """工具参数摘要：键 + 截断后的短值（不记录完整载荷与敏感文本）。"""
    summary: dict = {}
    for key, value in args.items():
        text = "" if value is None else str(value)
        summary[key] = text[:_TOOL_ARG_SUMMARY_LIMIT] + ("…" if len(text) > _TOOL_ARG_SUMMARY_LIMIT else "")
    return summary


def _guarded_tool_call(*, name: str, args: dict, calls: list[dict], counts: dict, limit: int, fn):
    """统一的工具重复调用门禁（只读工具与受控写工具 ``generate_draft`` 共用）。

    key = 工具名 + canonical JSON 参数；**先递增计数再判定**，超过 ``limit`` 时
    **不执行** ``fn``（因此不产生任何业务副作用），返回 ``(None, True)`` 表示被拦截。
    成功调用会写入 ``calls`` 审计（参数为规范化后的 key 参数）。

    每次调用都写一条语义 span（工具名 / 参数摘要 / 计数 / 结果规模 / 稳定错误码），
    供决策链追踪；span 只记摘要，不记完整结果、完整 Prompt 或密钥。
    """
    from app.observability import record_span

    key = _tool_key(name, args)
    counts[key] = int(counts.get(key, 0)) + 1
    summary = _tool_args_summary(args)
    if counts[key] > limit:
        logger.warning("工具重复调用超限转人工：tool=%s count=%s limit=%s", name, counts[key], limit)
        record_span(
            name="agent.tool",
            input_={"tool": name, "args": summary},
            metadata={
                "tool": name,
                "count": counts[key],
                "limit": limit,
                "error_code": "TOOL_DUPLICATE_BLOCKED",
            },
            level="WARNING",
        )
        return None, True
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 失败也要留 trace，再按原样抛出
        record_span(
            name="agent.tool",
            input_={"tool": name, "args": summary},
            metadata={"tool": name, "count": counts[key], "error_code": type(exc).__name__},
            level="ERROR",
        )
        raise
    tools.record(calls, name, args, result)
    record_span(
        name="agent.tool",
        input_={"tool": name, "args": summary},
        output={"result_size": _tool_result_size(result)},
        metadata={"tool": name, "count": counts[key], "result_size": _tool_result_size(result)},
    )
    return result, False


def _with_guard(node_name: str, fn):
    """节点统一守卫：进入即记步数；超上限或已 loop_blocked 时短路为 escalate。

    短路时**不执行**节点实际工作，因此不会继续调用只读工具或 ``generate_draft``。
    步数取自 state（由 checkpoint 恢复），重连不会丢失计数。

    ``node_name`` 必须是**图节点名**（如 ``classify``）而不是函数名（``node_classify``），
    以便语义追踪与 LangGraph 图结构对齐。
    """

    @wraps(fn)
    def wrapped(state: AgentState) -> dict:
        from app.observability import record_span

        count = int(state.get("step_count", 0) or 0) + 1
        limit = int(get_settings().agent_max_steps)
        blocked = bool(state.get("loop_blocked"))
        if blocked or count > limit:
            logger.warning(
                "Agent 门禁触发转人工：node=%s step=%s limit=%s loop_blocked=%s",
                node_name,
                count,
                limit,
                blocked,
            )
            record_span(
                name="agent.node",
                input_={"node": node_name},
                metadata={
                    "node": node_name,
                    "step_count": count,
                    "limit": limit,
                    "escalated": True,
                    "loop_blocked": blocked,
                },
                level="WARNING",
            )
            return {
                "step_count": count,
                "loop_blocked": True,
                "outcome": "escalated",
                "needs_approval": False,
                "response": ESCALATE_RESPONSE,
            }
        update = dict(fn(state) or {})
        update["step_count"] = count
        # 节点级语义追踪：记录节点执行顺序（node + step_count）与节点产出的 outcome
        record_span(
            name="agent.node",
            input_={"node": node_name},
            output={"outcome": update.get("outcome")},
            metadata={
                "node": node_name,
                "step_count": count,
                "outcome": update.get("outcome"),
                "intent": update.get("intent"),
                "degradation_reason": update.get("degradation_reason"),
                "loop_blocked": bool(update.get("loop_blocked")),
            },
        )
        return update

    return wrapped


def node_classify(state: AgentState) -> dict:
    text = state.get("user_input", "")
    from app.db import get_session_factory

    factory = get_session_factory()
    mode = _llm_mode()
    # 降级原因必须显式记录（不允许静默降级）：offline 是主动选择，其余是真实降级。
    degradation_reason: str | None = None
    if mode == "offline":
        degradation_reason = "LLM_MODE_OFFLINE"
    elif not _llm_configured():
        degradation_reason = "LLM_NOT_CONFIGURED"
    result: dict = {
        "intent": "other",
        "params": {},
        "missing_params": [],
        "clarification": None,
        "offline": not _llm_enabled(),
        "budget_note": False,
        "degradation_reason": degradation_reason,
    }
    with factory() as session:
        parsed = offline.parse_params(text, session)
        result.update(
            {
                "intent": parsed.intent,
                "params": parsed.params,
                "missing_params": offline.normalize_missing(parsed.missing),
                "clarification": parsed.clarification,
                "budget_note": parsed.budget_note,
            }
        )
        if _llm_enabled():
            from app.agent.llm import LLMError, llm_parse_params

            try:
                llm_result = llm_parse_params(text)
                result["intent"] = llm_result.get("intent", parsed.intent)
                # 归一化商品范围：分类名展开为具体 SKU（LLM 返回"紧固件"等分类时）
                llm_products = list(llm_result.get("params", {}).get("products", []) or [])
                expanded = _expand_products(session, llm_products)
                params = dict(llm_result.get("params", {}))
                params["products"] = expanded if expanded else llm_products
                result["params"] = params
                missing = offline.normalize_missing(list(llm_result.get("missing", parsed.missing)))
                if not params.get("products"):
                    missing = list(dict.fromkeys(missing + ["product"]))
                result["missing_params"] = missing
                result["clarification"] = llm_result.get("clarification")
                result["offline"] = False
                result["degradation_reason"] = None
            except LLMError as exc:
                # 稳定降级原因：LLM_TIMEOUT / LLM_INVALID_RESPONSE / LLM_UNAVAILABLE / LLM_NOT_CONFIGURED
                logger.warning("真实 LLM 失败（%s），回退 OFFLINE: %s", exc.code, exc)
                result["offline"] = True
                result["degradation_reason"] = exc.code
            except Exception as exc:  # noqa: BLE001 未分类异常也回退，但不得静默
                logger.warning("LLM 解析异常，回退 OFFLINE: %s", exc)
                result["offline"] = True
                result["degradation_reason"] = "LLM_UNAVAILABLE"
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
    if state.get("outcome") == "escalated":
        return "escalate"
    intent = state.get("intent", "other")
    if intent != "replenish":
        return "respond"
    if state.get("missing_params"):
        return "clarify"
    return "gather_evidence"


def route_after_evidence(state: AgentState) -> str:
    """证据节点后：门禁触发则转人工，否则进入受控草稿。"""
    return "escalate" if state.get("outcome") == "escalated" else "draft"


def route_after_draft(state: AgentState) -> str:
    """草稿节点后：门禁触发则转人工，否则进入审批等待点。"""
    return "escalate" if state.get("outcome") == "escalated" else "wait"


def node_clarify(state: AgentState) -> dict:
    missing = list(state.get("missing_params", []) or [])
    missing_label = offline.missing_label(missing) or "仓库、SKU 或商品范围、规划周期"
    # 自然业务语言引导：指出缺什么、怎么补、给可复制的完整句式
    response = (
        f"还需要补充：{missing_label}。\n"
        "请提供以下信息：\n"
        "① 仓库：如“华东仓”或“华南仓”；\n"
        "② SKU 或商品分类：如“SKU-E01”或“紧固件”；\n"
        "③ 规划周期：7 天 / 14 天 / 30 天。\n"
        f"例如：{offline.REPLENISH_EXAMPLE}"
    )
    if state.get("budget_note"):
        response += "\n" + offline.BUDGET_NOTE
    return {
        "response": response,
        "outcome": "clarified",
        "missing_params": missing,
    }


def node_gather_evidence(state: AgentState) -> dict:
    """只读工具编排：库存、在途、供应商关系、规则候选（证据，不进计算）。

    同一会话内「同一工具 + 同一规范化参数」重复超过 ``AGENT_TOOL_DUPLICATE_LIMIT``
    时：写告警日志、置 ``loop_blocked``、停止后续工具调用并转人工（不再调用工具）。
    不同参数不会被误判为重复（key 含规范化参数）。
    """
    from app.observability import record_span

    params = state.get("params", {})
    warehouse_id = params.get("warehouse_id")
    products = list(params.get("products", []) or [])
    calls = list(state.get("tool_calls", []))
    counts: dict = dict(state.get("tool_call_counts", {}) or {})
    limit = int(get_settings().agent_tool_duplicate_limit)
    loop_blocked = False

    def _call(name: str, args: dict, fn):
        """受门禁保护的工具调用：先计数判定，超限则不执行并置 loop_blocked。"""
        nonlocal loop_blocked
        result, blocked = _guarded_tool_call(
            name=name, args=args, calls=calls, counts=counts, limit=limit, fn=fn
        )
        if blocked:
            loop_blocked = True
        return result

    for pid in products:
        assert isinstance(warehouse_id, str)  # 路由已保证必要参数完整
        _call(
            "get_inventory",
            {"warehouse_id": warehouse_id, "product_id": pid},
            lambda pid=pid: tools.get_inventory(warehouse_id, pid),
        )
        if not loop_blocked:
            _call(
                "get_supplier_options",
                {"product_id": pid},
                lambda pid=pid: tools.get_supplier_options(pid),
            )
        if not loop_blocked:
            _call(
                "get_demand_history",
                {"warehouse_id": warehouse_id, "product_id": pid},
                lambda pid=pid: tools.get_demand_history(warehouse_id, pid),
            )
        if loop_blocked:
            break

    if loop_blocked:
        return {
            "tool_calls": calls,
            "tool_call_counts": counts,
            "loop_blocked": True,
            "outcome": "escalated",
            "response": ESCALATE_RESPONSE,
        }

    query = "安全库存 补货 规则"
    # 计数参数与实际检索上下文一致（含仓库与商品范围）：
    # 否则不同请求共用固定 query，会被误判成"相同工具相同参数"的重复调用。
    search_args = {"query": query, "warehouse_id": warehouse_id, "products": products}
    evidence = _call("search_rules", search_args, lambda: tools.search_rules(f"{query} {warehouse_id}"))
    if loop_blocked or evidence is None:
        return {
            "tool_calls": calls,
            "tool_call_counts": counts,
            "loop_blocked": True,
            "outcome": "escalated",
            "response": ESCALATE_RESPONSE,
        }
    hits = evidence.get("hits", []) or []
    record_span(
        name="rag.search_rules",
        input_={"query": query, "warehouse_id": warehouse_id},
        output={"hit_count": len(hits)},
        metadata={
            "products": products,
            # RAG 引用的文档块 ID（可追溯证据）；无命中时记录空列表与 has_evidence=false，不虚构 citation
            "cited_source_chunk_ids": [h.get("source_chunk_id") for h in hits],
            "cited_document_ids": sorted({h.get("document_id") for h in hits if h.get("document_id")}),
            "has_evidence": bool(hits),
        },
    )
    return {
        "tool_calls": calls,
        "tool_call_counts": counts,
        "rule_evidence": evidence.get("hits", []),
    }


def node_draft(state: AgentState) -> dict:
    """受控草稿工具：领域服务校验并落库；提交待审批后 interrupt 暂停会话。

    该**写工具与只读工具共用同一重复调用门禁**（key = 工具名 + canonical JSON 参数）：
    同一会话中相同参数超过 ``AGENT_TOOL_DUPLICATE_LIMIT`` 时**不执行**领域服务
    （因此不产生新的业务副作用），置 ``loop_blocked`` 并转人工；被拦截时状态中不保留
    任何看似成功的草稿 / 计划结果。计数随 state 写入 checkpoint，断线续传不重置。
    """
    from app.agent.blocked_hints import blocked_line_summary
    from app.observability import record_span

    params = state.get("params", {})
    calls = list(state.get("tool_calls", []))
    counts: dict = dict(state.get("tool_call_counts", {}) or {})
    limit = int(get_settings().agent_tool_duplicate_limit)
    # key 参数规范化：products 排序，保证「同一集合不同顺序」被视为同一请求
    key_args = {
        "actor_id": state.get("actor_id"),
        "warehouse_id": params["warehouse_id"],
        "products": sorted(params["products"]),
        "requested_window": params["requested_window"],
    }
    try:
        draft, blocked = _guarded_tool_call(
            name="generate_draft",
            args=key_args,
            calls=calls,
            counts=counts,
            limit=limit,
            fn=lambda: tools.generate_draft(
                actor_id=state.get("actor_id", "operator"),
                warehouse_id=params["warehouse_id"],
                products=params["products"],
                requested_window=params["requested_window"],
                thread_id=state.get("thread_id"),
            ),
        )
    except StockMindError as exc:
        tools.record(calls, "generate_draft", key_args, None, exc.message)
        record_span(
            name="domain.generate_draft",
            input_=params,
            output=None,
            metadata={"error": exc.code, "message": exc.message[:300]},
            level="ERROR",
        )
        # 防重/并发类错误给出明确下一步；其余阻断按错误码提示
        hint = blocked_line_summary("", exc.code, exc.message, exc.detail)
        return {
            "tool_calls": calls,
            "tool_call_counts": counts,
            "draft_result": None,
            "outcome": "blocked",
            "blocked_lines": [hint],
            "response": f"无法生成草稿：{exc.message}。下一步：{hint['next_step']}",
        }

    if blocked:
        # 门禁拦截：领域服务未被调用，不残留草稿 / 计划结果，统一转人工
        return {
            "tool_calls": calls,
            "tool_call_counts": counts,
            "loop_blocked": True,
            "outcome": "escalated",
            "draft_result": None,
            "plan_id": None,
            "needs_approval": False,
            "response": ESCALATE_RESPONSE,
        }

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
            "tool_call_counts": counts,
            "draft_result": draft,
            "outcome": "blocked",
            "blocked_lines": summaries,
            "response": f"本次没有可提交的补货建议。阻断原因：{reason}",
        }
    # 草稿已落库并提交待审批：先在状态中记录，再进入 wait 节点 interrupt 暂停
    response = f"已生成补货草稿（计划 {draft['plan_id']}）并提交待审批，会话已暂停；审批人处理后会话自动恢复。"
    if state.get("budget_note"):
        response += "\n" + offline.BUDGET_NOTE
    return {
        "tool_calls": calls,
        "tool_call_counts": counts,
        "draft_result": draft,
        "plan_id": draft["plan_id"],
        "needs_approval": True,
        "outcome": "draft_created",
        "response": response,
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
    response = None
    if intent == "query":
        response = _query_response(text)
    elif intent == "explain":
        response = (
            "补货建议依据《补货策略总则》《安全库存规则》等文档，按确定性公式计算；"
            "请查看计划明细中的公式、数据快照与规则引用（不可信检索文本不改变系统边界）。"
        )
    else:
        response = "我只能处理补货、库存查询与规则解释；该请求超出范围，无法执行。"
    if state.get("budget_note"):
        response += "\n" + offline.BUDGET_NOTE
    return {"response": response, "outcome": "answered"}


def node_escalate(state: AgentState) -> dict:
    """转人工出口：步数超限或工具重复调用超限时的稳定终止节点。

    只返回业务语言与稳定 ``outcome``，不再调用任何工具，也不产生业务副作用。
    """
    reason = "tool_duplicate" if state.get("loop_blocked") else "max_steps"
    logger.warning(
        "Agent 转人工：reason=%s step_count=%s limit=%s",
        reason,
        state.get("step_count"),
        get_settings().agent_max_steps,
    )
    return {
        "outcome": "escalated",
        "needs_approval": False,
        "loop_blocked": True,
        "response": ESCALATE_RESPONSE,
    }


def _query_response(text: str) -> str:
    """通用库存查询：解析文本中的仓库别名与 SKU，返回该 SKU 当前库存业务信息。

    示例句式：“帮我检查华南仓 SKU-E08 未来7天库存”（query 意图，V1 只回答当前库存）。
    """
    from app.agent import tools as _tools

    wh = None
    for alias, wid in offline._WAREHOUSE_ALIASES.items():
        if alias in text:
            wh = wid
            break
    sku_matches = re.findall(r"SKU-?[A-Z0-9]+", text.upper().replace(" ", ""))
    sku = None
    if sku_matches:
        raw = sku_matches[0]
        sku = raw if raw.startswith("SKU-") else f"SKU-{raw.removeprefix('SKU')}"
        if not sku.startswith("SKU-"):
            sku = f"SKU-{raw}"
    if wh and sku:
        return str(_tools.get_inventory(wh, sku))
    if sku:
        return f"请指定仓库后再查询（例如：帮我检查华东仓 {sku} 的库存）。"
    return "请指定仓库与 SKU 后再查询（例如：华东仓 SKU-E01 库存）。"


# ---------------------------------------------------------------- 图构造


def build_graph():
    builder = StateGraph(AgentState)
    # 除 escalate 外的节点统一加门禁包装：进入即记步数，超限/循环时短路转人工
    builder.add_node("classify", _with_guard("classify", node_classify))
    builder.add_node("clarify", _with_guard("clarify", node_clarify))
    builder.add_node("gather_evidence", _with_guard("gather_evidence", node_gather_evidence))
    builder.add_node("draft", _with_guard("draft", node_draft))
    builder.add_node("wait", _with_guard("wait", node_wait))
    builder.add_node("finalize", _with_guard("finalize", node_finalize))
    builder.add_node("respond", _with_guard("respond", node_respond))
    # escalate 是终端稳定出口，不再包装（避免被短路逻辑覆盖其自身日志）
    builder.add_node("escalate", node_escalate)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        route_after_classify,
        {
            "clarify": "clarify",
            "gather_evidence": "gather_evidence",
            "respond": "respond",
            "escalate": "escalate",
        },
    )
    builder.add_conditional_edges(
        "gather_evidence",
        route_after_evidence,
        {"draft": "draft", "escalate": "escalate"},
    )
    builder.add_conditional_edges(
        "draft",
        route_after_draft,
        {"wait": "wait", "escalate": "escalate"},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("respond", END)
    builder.add_edge("wait", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("escalate", END)
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
        # 每个新 turn 重置步数（避免多轮对话累积被误判超限）；
        # tool_call_counts 不重置：同一会话的工具重复计数跨 turn 累积（由 checkpoint 保存）。
        "step_count": 0,
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
                "degradation_reason": result.get("degradation_reason"),
                "step_count": result.get("step_count"),
                "loop_blocked": result.get("loop_blocked"),
                "tool_count": len(result.get("tool_calls", [])),
            },
        )
        flush()
        return result, interrupted


def _node_summary(node: str, acc: dict) -> dict:
    """节点完成时的轻量摘要（供 SSE node_end 展示进度与门禁状态）。"""
    summary: dict = {"node": node, "step_count": acc.get("step_count")}
    if node == "classify":
        summary["intent"] = acc.get("intent")
        summary["offline"] = acc.get("offline")
        summary["degradation_reason"] = acc.get("degradation_reason")
    elif node == "gather_evidence":
        summary["tool_count"] = len(acc.get("tool_calls", []))
    elif node == "draft":
        draft = acc.get("draft_result")
        if draft and draft.get("created"):
            summary["created"] = True
            summary["plan_id"] = draft.get("plan_id")
        else:
            summary["outcome"] = acc.get("outcome", "blocked")
    elif node == "wait":
        summary["interrupted"] = bool(acc.get("needs_approval"))
    if acc.get("loop_blocked"):
        summary["loop_blocked"] = True
    return summary


def _events_from_chunk(chunk: dict, acc: dict, seen: list[int]):
    """把一个 ``graph.stream`` chunk 转成 ``(event, data)`` 序列。

    原地更新 ``acc``（累积状态）与 ``seen[0]``（tool_call 增量游标）。
    """
    if "__interrupt__" in chunk:
        # wait 节点 interrupt：draft 节点的状态已写入 acc
        if acc.get("needs_approval"):
            yield "interrupted", {
                "status": "pending_approval",
                "plan_id": acc.get("plan_id"),
                "step_count": acc.get("step_count"),
            }
        return
    for node, update in chunk.items():
        if not isinstance(update, dict):
            continue
        acc.update(update)
        # 工具调用增量（gather_evidence 节点逐个写入）
        calls = acc.get("tool_calls", [])
        while seen[0] < len(calls):
            yield "tool_call", calls[seen[0]]
            seen[0] += 1
        if node == "draft":
            draft = acc.get("draft_result")
            if draft and draft.get("created"):
                yield "draft_created", {"plan_id": draft.get("plan_id")}
        yield "node_end", _node_summary(node, acc)


async def _astream_graph(
    *,
    thread_id: str,
    actor_id: str | None,
    graph_input,
    acc_init: dict | None,
    trace_name: str,
    trace_meta: dict,
    tags: list[str],
    trace_input: str | None = None,
    startup_event: tuple[str, dict] | None = None,
):
    """内部：把 ``graph.astream`` 的事件流转成 ``(event, data)`` 供 SSE 层消费。

    统一进入 ``turn_trace`` 并在结束时 ``flush``（无凭证时安全 no-op）。异常记入
    观测后继续抛出，由传输层（``app.streaming.stream_agent_turn``）转为 error + done。
    """
    from app.observability import flush, mark_error, record_span, turn_trace

    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    acc: dict = dict(acc_init or {})
    seen = [0]

    with turn_trace(
        name=trace_name,
        thread_id=thread_id,
        actor_id=actor_id,
        input_=trace_input,
        metadata=trace_meta,
        tags=tags,
    ):
        try:
            if startup_event is not None:
                yield startup_event

            # 同步 checkpointer（PostgresSaver）不支持异步 API（aget_tuple -> NotImplementedError），
            # 因此在工作线程执行同步 graph.stream，chunk 经 asyncio.Queue 桥接到本异步生成器：
            # 既不阻塞事件循环，也不需要维护异步连接池。
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            end_marker = object()

            def _produce() -> None:
                try:
                    for chunk in graph.stream(graph_input, config=config):
                        loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
                except BaseException as exc:  # noqa: BLE001 交由消费端按原类型抛出
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, (end_marker, None))

            threading.Thread(
                target=_produce,
                name=f"agent-stream-{thread_id[:8]}",
                daemon=True,
            ).start()

            while True:
                kind, payload = await queue.get()
                if kind is end_marker:
                    break
                if kind == "error":
                    raise payload
                for event, data in _events_from_chunk(payload, acc, seen):
                    yield event, data
        except GraphInterrupt:  # pragma: no cover 版本兼容兜底（部分版本抛异常而非 __interrupt__ chunk）
            if acc.get("needs_approval"):
                yield "interrupted", {
                    "status": "pending_approval",
                    "plan_id": acc.get("plan_id"),
                    "step_count": acc.get("step_count"),
                }
        except Exception as exc:  # noqa: BLE001 记录观测后继续抛出（不掩盖业务错误）
            mark_error(f"{type(exc).__name__}: {exc}")
            record_span(name="agent.error", metadata={"error": str(exc)[:500]}, level="ERROR")
            raise
        finally:
            flush()

        interrupted = bool(acc.get("needs_approval"))
        record_span(
            name="agent.turn.summary",
            metadata={
                "intent": acc.get("intent"),
                "plan_id": acc.get("plan_id"),
                "interrupted": interrupted,
                "offline": acc.get("offline"),
                "degradation_reason": acc.get("degradation_reason"),
                "step_count": acc.get("step_count"),
                "loop_blocked": acc.get("loop_blocked"),
                "tool_count": len(acc.get("tool_calls", [])),
            },
        )
        yield "message", {
            "content": acc.get("response", ""),
            "offline": acc.get("offline", True),
            "degradation_reason": acc.get("degradation_reason"),
            "step_count": acc.get("step_count"),
            "loop_blocked": bool(acc.get("loop_blocked")),
            "outcome": acc.get("outcome"),
            "blocked_lines": acc.get("blocked_lines"),
            "missing_params": acc.get("missing_params"),
        }
        yield "done", {
            "interrupted": interrupted,
            "plan_id": acc.get("plan_id"),
            "outcome": acc.get("outcome"),
            "step_count": acc.get("step_count"),
            "loop_blocked": bool(acc.get("loop_blocked")),
            "degradation_reason": acc.get("degradation_reason"),
        }


async def astream_turn(thread_id: str, actor_id: str, user_input: str):
    """真流式执行一轮会话（新用户输入），逐节点产出 ``(event, data)`` 事件。

    与 ``run_turn`` 语义一致（interrupted 判定、最终 state 字段相同），但通过
    ``graph.astream`` 逐节点产出进度事件供 SSE 实时推送；事件序号由 API 层统一
    分配并缓存，供断线续传（见 ``app.streaming``）。

    事件：agent_start / tool_call / draft_created / interrupted / node_end /
    message / done，并携带 ``degradation_reason`` / ``step_count`` / ``loop_blocked``
    等可审计字段（不静默降级）。

    V1 为离线优先确定性图，节点多为毫秒级同步计算，流式粒度为节点级；
    token 级流式仅在真实 LLM 启用路径（node_classify 内 llm_parse_params）才有意义。
    """
    mode = _llm_mode()
    startup_degradation = (
        "LLM_MODE_OFFLINE" if mode == "offline" else (None if _llm_configured() else "LLM_NOT_CONFIGURED")
    )
    initial: AgentState = {
        "thread_id": thread_id,
        "actor_id": actor_id,
        "user_input": user_input,
        "tool_calls": [],
        "rule_evidence": [],
        # 每个新 turn 重置步数；tool_call_counts 跨 turn 累积（同一会话重复检测）
        "step_count": 0,
    }
    async for item in _astream_graph(
        thread_id=thread_id,
        actor_id=actor_id,
        graph_input=initial,
        acc_init=initial,
        trace_name="agent.turn",
        trace_meta={"intent_source": "astream_turn"},
        trace_input=user_input,
        tags=["agent", "v1", "stream"],
        startup_event=(
            "agent_start",
            {
                "thread_id": thread_id,
                "offline": not _llm_enabled(),
                "llm_mode": mode,
                "degradation_reason": startup_degradation,
            },
        ),
    ):
        yield item


async def astream_resume(thread_id: str, plan_id: str, decision_version: int):
    """流式恢复会话：读取数据库**已提交**的业务决定并解释结果。

    只恢复对话流程（``Command(resume=...)``），**不执行审批、不创建采购单、不产生
    任何业务副作用**（宪法第七条）；恢复键为 ``thread_id + plan_id + decision_version``。
    """
    payload = {
        "status": _decision_status(plan_id),
        "plan_id": plan_id,
        "decision_version": decision_version,
    }
    async for item in _astream_graph(
        thread_id=thread_id,
        actor_id=None,
        graph_input=Command(resume=payload),
        acc_init=None,
        trace_name="agent.resume",
        trace_meta={"plan_id": plan_id, "decision_version": decision_version},
        tags=["agent", "v1", "stream", "resume"],
        startup_event=(
            "agent_start",
            {
                "thread_id": thread_id,
                "resume": True,
                "plan_id": plan_id,
                "decision_version": decision_version,
            },
        ),
    ):
        yield item


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
