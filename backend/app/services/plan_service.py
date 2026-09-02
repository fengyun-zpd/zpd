"""补货计划领域服务（需求 6.1 / 架构 7.1 / ADR 决策 5、8）。

职责：草稿生成（确定性计算 + decision_input_hash + 活动建议防重）、
提交待审批、逐条审批/排除/整单驳回（PLAN_STALE 校验）、修订替代、按供应商建单。

计算顺序（宪法第十条）：规则校验 -> 预测 -> 选择供应商 -> 计算补货量。
所有影响补货判断的事务按 (warehouse_id, product_id) 排序取得 advisory lock。
所有命令使用统一幂等守卫（草稿工具以 operation_id 为幂等键）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import (
    CMD_APPROVE,
    CMD_CREATE_PO,
    CMD_DRAFT,
    CMD_SUPERSEDE,
    HASH_SCHEMA_VERSION,
    LINE_BLOCKED,
    LINE_VALID,
    PLAN_APPROVED,
    PLAN_PENDING_APPROVAL,
    PLAN_REJECTED,
    PLAN_SUPERSEDED,
    RULE_REPLENISHMENT_POLICY,
)
from app.errors import (
    ActiveReplenishmentExistsError,
    BlockedInputError,
    InvalidStateTransitionError,
    NotFoundError,
    PlanStaleError,
    ValidationError,
    VersionConflictError,
)
from app.models.agent import WorkflowResume
from app.models.inventory import Product, Warehouse
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine, SupplierProduct
from app.models.replenishment import PlanLine, ReplenishmentPlan
from app.services.alert_service import close_open_alert, upsert_open_alert
from app.services.audit import write_audit
from app.services.forecast import forecast_daily_demand
from app.services.hashing import decision_input_hash, payload_hash
from app.services.idempotency import IdempotencyGuard, replay_failure
from app.services.inventory_service import business_date as _business_date
from app.services.inventory_service import (
    get_demand_series,
    get_inbound_remaining,
    get_quant_summary,
)
from app.services.locks import acquire_warehouse_product_locks
from app.services.replenishment import ReplenishmentInput, compute_replenishment
from app.services.rules import resolve_rule, resolve_safety_stock_rule
from app.services.supplier import SupplierCandidate, select_supplier

ALGO_VERSION = "stockmind-replenish-v1"


@dataclass
class LineOutcome:
    product_id: str
    flag: str
    line_id: str | None = None
    order_qty: int = 0
    blocked_code: str | None = None
    blocked_reason: str | None = None
    supplier_id: str | None = None
    daily_forecast: Decimal | None = None


@dataclass
class DraftResult:
    plan_id: str | None
    lines: list[LineOutcome] = field(default_factory=list)
    created: bool = False


def _transition(plan: ReplenishmentPlan, target: str, allowed: dict[str, tuple[str, ...]]) -> None:
    if target not in allowed.get(plan.status, ()):
        raise InvalidStateTransitionError(f"非法计划状态迁移 {plan.status} -> {target}")


# ---------------------------------------------------------------- 决策输入哈希


def build_line_hash_inputs(
    *,
    warehouse_id: str,
    product_id: str,
    business_date: date,
    requested_window: int,
    on_hand: int,
    reserved: int,
    quant_version: int,
    inbound: list[dict],
    demand_cutoff_date: date,
    rules: list[dict],
    supplier: dict | None,
) -> dict:
    """规范化决策输入（需求 5.3 / 6.1.1）。"""
    return {
        "warehouse_id": warehouse_id,
        "product_id": product_id,
        "business_date": business_date,
        "requested_window": requested_window,
        "on_hand": on_hand,
        "reserved": reserved,
        "quant_version": quant_version,
        "inbound": inbound,
        "demand_cutoff_date": demand_cutoff_date,
        "rules": rules,
        "supplier": supplier,
        "algorithm_version": ALGO_VERSION,
    }


def _plan_window(session: Session, plan_id: str) -> int:
    plan = session.get(ReplenishmentPlan, plan_id)
    return plan.requested_window if plan else 14


def _inbound_snapshot(session: Session, warehouse_id: str, product_id: str) -> list[dict]:
    from app.constants import INBOUND_PO_STATES

    rows = session.execute(
        select(PurchaseOrderLine, PurchaseOrder)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .where(
            PurchaseOrder.warehouse_id == warehouse_id,
            PurchaseOrderLine.product_id == product_id,
            PurchaseOrder.status.in_(INBOUND_PO_STATES),
        )
    ).all()
    return [
        {
            "po_id": po.id,
            "po_status": po.status,
            "line_id": pol.id,
            "remaining": pol.order_qty - pol.received_qty,
        }
        for pol, po in rows
    ]


def recompute_line_hash(session: Session, line: PlanLine) -> list[str]:
    """事务中重算 decision_input_hash，返回变化字段列表（空 = 新鲜）。"""
    warehouse = session.get(Warehouse, line.warehouse_id)
    if warehouse is None:  # pragma: no cover
        raise NotFoundError(f"仓库不存在 {line.warehouse_id}")
    bdate = _business_date(warehouse.timezone)

    on_hand, reserved, quant_version = get_quant_summary(session, line.warehouse_id, line.product_id)
    inbound = _inbound_snapshot(session, line.warehouse_id, line.product_id)

    rules: list[dict] = []
    for ref in line.rule_refs or []:
        if isinstance(ref, dict) and "rule_id" in ref:
            rules.append({k: ref[k] for k in ("rule_id", "version", "rule_type", "scope") if k in ref})

    supplier = None
    if line.supplier_id:
        rel = session.scalar(
            select(SupplierProduct).where(
                SupplierProduct.supplier_id == line.supplier_id,
                SupplierProduct.product_id == line.product_id,
            )
        )
        if rel is not None:
            supplier = {
                "supplier_id": line.supplier_id,
                "relation_id": rel.id,
                "version": rel.version,
            }

    fresh = build_line_hash_inputs(
        warehouse_id=line.warehouse_id,
        product_id=line.product_id,
        business_date=bdate,
        requested_window=_plan_window(session, line.plan_id),
        on_hand=on_hand,
        reserved=reserved,
        quant_version=quant_version,
        inbound=inbound,
        demand_cutoff_date=line.demand_cutoff_date or bdate,
        rules=rules,
        supplier=supplier,
    )
    new_hash = decision_input_hash(fresh)
    changed: list[str] = []
    if new_hash != line.decision_input_hash:
        if line.on_hand != on_hand:
            changed.append(f"on_hand({line.on_hand}->{on_hand})")
        if line.reserved != reserved:
            changed.append(f"reserved({line.reserved}->{reserved})")
        if int(line.inbound) != sum(int(i["remaining"]) for i in inbound):
            changed.append("inbound")
        if supplier is not None and (line.supplier_rel_version or 0) != supplier.get("version"):
            changed.append("supplier_relation_version")
        if not changed:
            changed.append("decision_input_hash")
    return changed


# ---------------------------------------------------------------- 单 SKU 计算


def _compute_line(
    session: Session,
    *,
    warehouse: Warehouse,
    product: Product,
    requested_window: int,
    bdate: date,
) -> tuple[dict | None, LineOutcome]:
    """计算单个 SKU 的补货建议（纯计算 + 证据，不落库）。"""
    wh_id, pid = warehouse.id, product.id

    # 1) 规则校验
    safety = resolve_safety_stock_rule(
        session,
        warehouse_id=wh_id,
        product_id=pid,
        category=product.category,
        business_date=bdate,
    )
    if safety.blocked or safety.rule is None:
        return None, LineOutcome(
            product_id=pid,
            flag=LINE_BLOCKED,
            blocked_code=safety.block_code or "no_rule",
            blocked_reason=safety.reason or "缺少适用的固定安全库存规则",
        )
    policy = resolve_rule(
        session,
        warehouse_id=wh_id,
        product_id=pid,
        category=product.category,
        business_date=bdate,
        rule_type=RULE_REPLENISHMENT_POLICY,
    )
    if policy.blocked:
        return None, LineOutcome(
            product_id=pid,
            flag=LINE_BLOCKED,
            blocked_code="rule_conflict",
            blocked_reason=policy.reason,
        )
    review_period_days = int((policy.rule.params or {}).get("review_period_days", 0) or 0) if policy.rule else 0

    # 2) 预测
    series = get_demand_series(session, wh_id, pid, upto=bdate)
    forecast = forecast_daily_demand(series)

    # 3) 选择供应商（必须先于补货量计算）
    rels = session.scalars(select(SupplierProduct).where(SupplierProduct.product_id == pid)).all()
    candidates = [
        SupplierCandidate(
            supplier_id=r.supplier_id,
            business_priority=r.business_priority,
            lead_days=r.lead_days,
            price=r.price,
            minimum_order_qty=r.minimum_order_qty,
            pack_multiple=r.pack_multiple,
            enabled=r.enabled,
            effective_from=r.effective_from,
            effective_to=r.effective_to,
            currency="CNY",
            raw={"relation_id": r.id, "version": r.version},
        )
        for r in rels
    ]
    chosen, _, reason = select_supplier(candidates, bdate)
    chosen_rel: SupplierProduct | None = None
    if chosen is not None:
        chosen_rel = session.scalar(
            select(SupplierProduct).where(
                SupplierProduct.supplier_id == chosen.supplier_id,
                SupplierProduct.product_id == pid,
            )
        )

    # 4) 计算补货量
    on_hand, reserved, quant_version = get_quant_summary(session, wh_id, pid)
    inbound_remaining = get_inbound_remaining(session, wh_id, pid)
    try:
        result = compute_replenishment(
            ReplenishmentInput(
                requested_window=requested_window,
                daily_forecast=forecast.daily_forecast,
                fixed_safety_stock=safety.rule.safety_stock or Decimal("0"),
                lead_days=chosen_rel.lead_days if chosen_rel else 0,
                review_period_days=review_period_days,
                minimum_order_qty=chosen_rel.minimum_order_qty if chosen_rel else 0,
                pack_multiple=chosen_rel.pack_multiple if chosen_rel else 1,
                on_hand=on_hand,
                reserved=reserved,
                inbound=Decimal(inbound_remaining),
            )
        )
    except BlockedInputError as exc:
        return None, LineOutcome(
            product_id=pid,
            flag=LINE_BLOCKED,
            blocked_code="illegal_input",
            blocked_reason=str(exc),
        )

    if chosen is None:
        return None, LineOutcome(
            product_id=pid,
            flag=LINE_BLOCKED,
            blocked_code="no_supplier",
            blocked_reason=reason,
        )
    assert chosen_rel is not None  # chosen 非 None 时必有匹配的供应商关系

    # 5) 决策输入哈希与证据
    rule_refs = []
    for r in (safety.rule, policy.rule):
        if r is not None:
            rule_refs.append(
                {
                    "rule_id": r.id,
                    "version": r.version,
                    "rule_type": r.rule_type,
                    "scope": r.scope,
                    "source_chunk_id": r.source_chunk_id,
                }
            )
    supplier_ref = {
        "supplier_id": chosen.supplier_id,
        "relation_id": chosen_rel.id,
        "version": chosen_rel.version,
    }
    inbound_snapshot = _inbound_snapshot(session, wh_id, pid)
    hash_inputs = build_line_hash_inputs(
        warehouse_id=wh_id,
        product_id=pid,
        business_date=bdate,
        requested_window=requested_window,
        on_hand=on_hand,
        reserved=reserved,
        quant_version=quant_version,
        inbound=inbound_snapshot,
        demand_cutoff_date=bdate,
        rules=[{k: r[k] for k in ("rule_id", "version", "rule_type", "scope")} for r in rule_refs],
        supplier=supplier_ref,
    )
    line_hash = decision_input_hash(hash_inputs)

    line_dict = {
        "warehouse_id": wh_id,
        "product_id": pid,
        "flag": LINE_VALID,
        "daily_forecast": forecast.daily_forecast,
        "forecast_algorithm": forecast.algorithm,
        "mae": forecast.mae,
        "wape": forecast.wape,
        "fallback_reason": forecast.fallback_reason,
        "planning_window": result.planning_window,
        "coverage_demand": result.coverage_demand,
        "target_stock": result.target_stock,
        "available": result.available,
        "inbound": result.inbound,
        "net_demand": result.net_demand,
        "order_qty": result.order_qty,
        "fixed_safety_stock": safety.rule.safety_stock or Decimal("0"),
        "lead_days": chosen_rel.lead_days,
        "review_period_days": review_period_days,
        "minimum_order_qty": chosen_rel.minimum_order_qty,
        "pack_multiple": chosen_rel.pack_multiple,
        "supplier_id": chosen.supplier_id,
        "supplier_reason": reason,
        "on_hand": on_hand,
        "reserved": reserved,
        "decision_input_hash": line_hash,
        "hash_schema_version": HASH_SCHEMA_VERSION,
        "input_snapshot": {
            "on_hand": on_hand,
            "reserved": reserved,
            "quant_version": quant_version,
            "inbound": inbound_snapshot,
            "demand_cutoff_date": bdate.isoformat(),
        },
        "intermediate": {
            "available": str(result.available),
            "planning_window": result.planning_window,
            "coverage_demand": str(result.coverage_demand),
            "target_stock": str(result.target_stock),
            "net_demand": str(result.net_demand),
            "forecast": {
                "algorithm": forecast.algorithm,
                "daily": str(forecast.daily_forecast) if forecast.daily_forecast is not None else None,
                "mae": str(forecast.mae) if forecast.mae is not None else None,
                "wape": str(forecast.wape) if forecast.wape is not None else None,
                "fallback_reason": forecast.fallback_reason,
            },
        },
        "rule_refs": rule_refs,
        "algorithm_version": ALGO_VERSION,
        "demand_cutoff_date": bdate,
        "quant_version": quant_version,
        "supplier_rel_version": chosen_rel.version,
    }
    return line_dict, LineOutcome(
        product_id=pid,
        flag=LINE_VALID,
        order_qty=result.order_qty,
        supplier_id=chosen.supplier_id,
        daily_forecast=forecast.daily_forecast,
    )


# ---------------------------------------------------------------- 定时扫描的公开单 SKU 计算入口


def compute_line_for_sku(
    session: Session,
    *,
    warehouse: Warehouse,
    product: Product,
    requested_window: int,
) -> tuple[dict | None, LineOutcome]:
    """定时扫描入口：单 SKU 计算（含异常隔离，需求 2.2）。

    未分类运行异常 -> outcome.flag="failed"；预期业务问题 -> blocked。
    """
    from app.errors import StockMindError as _StockMindError

    bdate = _business_date(warehouse.timezone)
    try:
        return _compute_line(
            session,
            warehouse=warehouse,
            product=product,
            requested_window=requested_window,
            bdate=bdate,
        )
    except _StockMindError as exc:
        return None, LineOutcome(
            product_id=product.id,
            flag=LINE_BLOCKED,
            blocked_code=exc.code,
            blocked_reason=exc.message,
        )
    except Exception as exc:  # noqa: BLE001 未分类运行异常
        return None, LineOutcome(
            product_id=product.id,
            flag="failed",
            blocked_code="runtime_error",
            blocked_reason=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------- 草稿生成


def generate_draft(
    session: Session,
    *,
    warehouse_id: str,
    requested_window: int,
    actor_id: str,
    products: list[str],
    operation_id: str,
    thread_id: str | None = None,
    trigger_type: str = "manual",
    use_guard: bool = True,
) -> DraftResult:
    """生成补货草稿并提交待审批（受控草稿工具的唯一入口）。"""
    warehouse = session.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise NotFoundError(f"仓库不存在 {warehouse_id}")
    if requested_window not in (7, 14, 30):
        raise ValidationError(f"规划窗口只允许 7/14/30，收到 {requested_window}")
    products = list(dict.fromkeys(products))
    if not products:
        raise ValidationError("必须提供至少一个 SKU")
    missing = [pid for pid in products if session.get(Product, pid) is None]
    if missing:
        raise NotFoundError(f"商品不存在：{missing}")

    guard = None
    if use_guard:
        guard = IdempotencyGuard(
            session,
            principal_id=actor_id,
            command_type=CMD_DRAFT,
            aggregate_ref=f"draft:{warehouse_id}:{requested_window}",
            idempotency_key=operation_id,
            payload={
                "warehouse_id": warehouse_id,
                "requested_window": requested_window,
                "products": products,
            },
        )
        begin = guard.begin()
        if begin.action == "replay_success":
            return _draft_from_payload(begin.response)
        if begin.action == "replay_failure":
            replay_failure(begin)

    bdate = _business_date(warehouse.timezone)

    active = session.scalars(
        select(PlanLine).where(
            PlanLine.warehouse_id == warehouse_id,
            PlanLine.product_id.in_(products),
            PlanLine.active_for_dedupe.is_(True),
        )
    ).all()
    if trigger_type == "manual" and active:
        line = active[0]
        raise ActiveReplenishmentExistsError(
            "同一仓库/SKU 已存在活动建议，不能并行创建第二条",
            detail={
                "plan_id": line.plan_id,
                "plan_line_id": line.id,
                "product_id": line.product_id,
            },
        )

    acquire_warehouse_product_locks(session, [(warehouse_id, pid) for pid in products])

    plan = ReplenishmentPlan(
        warehouse_id=warehouse_id,
        thread_id=thread_id,
        actor_id=actor_id,
        trigger_type=trigger_type,
        status=PLAN_PENDING_APPROVAL,
        requested_window=requested_window,
        planning_date=bdate,
    )
    session.add(plan)
    session.flush()

    outcomes: list[LineOutcome] = []
    for pid in products:
        product = session.get(Product, pid)
        assert product is not None  # 前面已校验存在
        line_dict, outcome = _compute_line(
            session,
            warehouse=warehouse,
            product=product,
            requested_window=requested_window,
            bdate=bdate,
        )
        if line_dict is not None:
            try:
                with session.begin_nested():
                    line = PlanLine(plan_id=plan.id, active_for_dedupe=True, **line_dict)
                    session.add(line)
                    session.flush()
                    outcome.line_id = line.id
                close_open_alert(session, warehouse_id=warehouse_id, product_id=pid, blocker_code="*")
            except IntegrityError as exc:
                if trigger_type == "manual":
                    raise ActiveReplenishmentExistsError(
                        "同一仓库/SKU 已存在活动建议（并发创建被唯一约束拦截）",
                        detail={"product_id": pid},
                    ) from exc
                outcome = LineOutcome(
                    product_id=pid,
                    flag=LINE_BLOCKED,
                    blocked_code="ACTIVE_REPLENISHMENT_EXISTS",
                    blocked_reason="存在未落实的活动建议，本次扫描跳过",
                )
        if outcome.flag == LINE_BLOCKED:
            upsert_open_alert(
                session,
                alert_type="blocked",
                warehouse_id=warehouse_id,
                product_id=pid,
                blocker_code=outcome.blocked_code or "unknown",
                message=outcome.blocked_reason or "",
                payload={"plan_id": plan.id, "trigger_type": trigger_type},
            )
        outcomes.append(outcome)

    valid = [o for o in outcomes if o.flag == LINE_VALID]
    if not valid:
        session.delete(plan)
        session.flush()
        write_audit(
            session,
            actor_id=actor_id,
            action=CMD_DRAFT,
            entity_type="replenishment_plan",
            entity_id=None,
            after_state={"warehouse_id": warehouse_id, "blocked_count": len(outcomes)},
        )
        result = DraftResult(plan_id=None, lines=outcomes, created=False)
        if guard:
            guard.succeed(_draft_payload(result))
        return result

    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_DRAFT,
        entity_type="replenishment_plan",
        entity_id=plan.id,
        after_state={
            "warehouse_id": warehouse_id,
            "requested_window": requested_window,
            "line_count": len(valid),
            "blocked_count": len(outcomes) - len(valid),
        },
    )
    result = DraftResult(plan_id=plan.id, lines=outcomes, created=True)
    if guard:
        guard.succeed(_draft_payload(result), entity_type="replenishment_plan", entity_id=plan.id)
    return result


def _draft_from_payload(payload: dict) -> DraftResult:
    return DraftResult(
        plan_id=payload.get("plan_id"),
        created=payload.get("created", False),
        lines=[
            LineOutcome(
                product_id=item["product_id"],
                flag=item["flag"],
                line_id=item.get("line_id"),
                order_qty=item.get("order_qty", 0),
                blocked_code=item.get("blocked_code"),
                blocked_reason=item.get("blocked_reason"),
                supplier_id=item.get("supplier_id"),
            )
            for item in payload.get("lines", [])
        ],
    )


def _draft_payload(result: DraftResult) -> dict:
    return {
        "plan_id": result.plan_id,
        "created": result.created,
        "lines": [
            {
                "product_id": o.product_id,
                "flag": o.flag,
                "line_id": o.line_id,
                "order_qty": o.order_qty,
                "blocked_code": o.blocked_code,
                "blocked_reason": o.blocked_reason,
                "supplier_id": o.supplier_id,
            }
            for o in result.lines
        ],
    }


# ---------------------------------------------------------------- 审批 / 排除 / 驳回


def decide_plan(
    session: Session,
    *,
    plan_id: str,
    actor_id: str,
    mode: str,
    decisions: dict[str, str] | None = None,
    expected_version: int | None = None,
    idempotency_key: str = "",
) -> ReplenishmentPlan:
    """审批（逐条批准/排除）或整单驳回；手动计划写入持久化 workflow_resume。"""
    plan = session.get(ReplenishmentPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"计划不存在 {plan_id}")
    if expected_version is not None and plan.version != expected_version:
        raise VersionConflictError(f"计划版本不匹配：期望 {expected_version}，当前 {plan.version}")
    if plan.status != PLAN_PENDING_APPROVAL:
        raise InvalidStateTransitionError(f"只有待审批计划可以审批，当前状态 {plan.status}")

    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_APPROVE,
        aggregate_ref=plan_id,
        idempotency_key=idempotency_key,
        payload={"plan_id": plan_id, "mode": mode, "decisions": decisions or {}},
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        replayed = session.get(ReplenishmentPlan, begin.response.get("plan_id"))
        assert replayed is not None
        return replayed
    if begin.action == "replay_failure":
        replay_failure(begin)

    lines = session.scalars(select(PlanLine).where(PlanLine.plan_id == plan_id).order_by(PlanLine.product_id)).all()
    pairs = [(line.warehouse_id, line.product_id) for line in lines if line.flag == LINE_VALID]
    acquire_warehouse_product_locks(session, pairs)

    before = {"status": plan.status, "version": plan.version}

    if mode == "reject":
        for line in lines:
            if line.active_for_dedupe:
                line.active_for_dedupe = False
        plan.status = PLAN_REJECTED
        plan.version += 1
        after = {"status": plan.status, "version": plan.version}
        write_audit(
            session,
            actor_id=actor_id,
            action=CMD_APPROVE,
            entity_type="replenishment_plan",
            entity_id=plan.id,
            before_state=before,
            after_state=after,
        )
        _write_resume_if_manual(session, plan, "rejected")
        guard.succeed(
            {"plan_id": plan.id, "status": plan.status, "version": plan.version},
            entity_type="replenishment_plan",
            entity_id=plan.id,
        )
        return plan

    decisions = decisions or {}
    valid_lines = [line for line in lines if line.flag == LINE_VALID]
    if not valid_lines:
        raise InvalidStateTransitionError("计划没有可审批的有效明细")

    changed_report: dict[str, list[str]] = {}
    approved_ids: list[str] = []
    for line in valid_lines:
        if decisions.get(line.id) == "approve":
            approved_ids.append(line.id)
            changed = recompute_line_hash(session, line)
            if changed:
                changed_report[line.product_id] = changed
    if changed_report:
        raise PlanStaleError(
            "决策输入已变化，不审批、不部分执行、不静默重算；请排除变化明细或创建修订版",
            detail={"changed_lines": changed_report},
        )

    for line in valid_lines:
        if decisions.get(line.id) == "approve":
            continue
        line.flag = "excluded"
        line.active_for_dedupe = False
        line.exclusion_reason = decisions.get(line.id) if line.id in decisions else "审批人未勾选（默认排除）"

    if approved_ids:
        plan.status = PLAN_APPROVED
    else:
        plan.status = PLAN_REJECTED
    plan.version += 1

    after = {"status": plan.status, "version": plan.version, "approved": approved_ids}
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_APPROVE,
        entity_type="replenishment_plan",
        entity_id=plan.id,
        before_state=before,
        after_state=after,
    )
    _write_resume_if_manual(session, plan, plan.status)
    guard.succeed(
        {"plan_id": plan.id, "status": plan.status, "version": plan.version},
        entity_type="replenishment_plan",
        entity_id=plan.id,
    )
    return plan


def _write_resume_if_manual(session: Session, plan: ReplenishmentPlan, decision_status: str) -> None:
    if not plan.thread_id:
        return
    payload = {
        "thread_id": plan.thread_id,
        "plan_id": plan.id,
        "decision_version": plan.version,
        "decision_status": decision_status,
    }
    existing = session.scalar(
        select(WorkflowResume).where(
            WorkflowResume.thread_id == plan.thread_id,
            WorkflowResume.plan_id == plan.id,
            WorkflowResume.decision_version == plan.version,
        )
    )
    if existing is None:
        session.add(
            WorkflowResume(
                thread_id=plan.thread_id,
                plan_id=plan.id,
                decision_version=plan.version,
                status="pending",
                payload_hash=payload_hash(payload),
                hash_schema_version=HASH_SCHEMA_VERSION,
            )
        )


# ---------------------------------------------------------------- 按供应商建单


def create_purchase_orders(
    session: Session,
    *,
    plan_id: str,
    actor_id: str,
    expected_version: int | None = None,
    idempotency_key: str = "",
) -> list[PurchaseOrder]:
    """批准明细按供应商聚合创建采购单（全有或全无，需求 5.2 / 6.1）。"""
    plan = session.get(ReplenishmentPlan, plan_id)
    if plan is None:
        raise NotFoundError(f"计划不存在 {plan_id}")
    if expected_version is not None and plan.version != expected_version:
        raise VersionConflictError(f"计划版本不匹配：期望 {expected_version}，当前 {plan.version}")
    if plan.status != PLAN_APPROVED:
        raise InvalidStateTransitionError(f"只有已批准计划可以建单，当前状态 {plan.status}")

    approved = session.scalars(
        select(PlanLine).where(
            PlanLine.plan_id == plan_id,
            PlanLine.flag == LINE_VALID,
        )
    ).all()
    if not approved:
        raise ValidationError("计划没有已批准的有效明细")

    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_CREATE_PO,
        aggregate_ref=plan_id,
        idempotency_key=idempotency_key,
        payload={"plan_id": plan_id},
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        return begin.response.get("purchase_order_ids", [])
    if begin.action == "replay_failure":
        replay_failure(begin)

    acquire_warehouse_product_locks(session, [(line.warehouse_id, line.product_id) for line in approved])

    changed_report: dict[str, list[str]] = {}
    for line in approved:
        changed = recompute_line_hash(session, line)
        if changed:
            changed_report[line.product_id] = changed
    if changed_report:
        raise PlanStaleError(
            "已批准明细的决策输入已变化，不创建任何采购单；请排除变化明细或创建修订版",
            detail={"changed_lines": changed_report},
        )

    by_supplier: dict[str, list[PlanLine]] = {}
    for line in approved:
        # 净需求为 0 的明细不产生收货义务：不进入采购单（需求 5.3 订货量为 0 时不建单）
        if line.order_qty <= 0:
            continue
        by_supplier.setdefault(line.supplier_id or "", []).append(line)

    created: list[PurchaseOrder] = []
    for supplier_id, lines in sorted(by_supplier.items()):
        if not supplier_id:
            raise ValidationError("批准明细缺少供应商，无法建单")
        po = PurchaseOrder(
            warehouse_id=plan.warehouse_id,
            plan_id=plan.id,
            supplier_id=supplier_id,
            status="po_created",
        )
        session.add(po)
        session.flush()
        for line in lines:
            rel = session.scalar(
                select(SupplierProduct).where(
                    SupplierProduct.supplier_id == supplier_id,
                    SupplierProduct.product_id == line.product_id,
                )
            )
            if rel is None:  # pragma: no cover
                raise ValidationError(f"供应商关系缺失 supplier={supplier_id} product={line.product_id}")
            session.add(
                PurchaseOrderLine(
                    purchase_order_id=po.id,
                    plan_line_id=line.id,
                    product_id=line.product_id,
                    order_qty=line.order_qty,
                    received_qty=0,
                    unit_price=rel.price,
                )
            )
            line.active_for_dedupe = False  # 同一事务：建立唯一关联并解除活动状态
        created.append(po)

    plan.version += 1
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_CREATE_PO,
        entity_type="replenishment_plan",
        entity_id=plan.id,
        before_state={"status": plan.status},
        after_state={"purchase_order_ids": [po.id for po in created], "version": plan.version},
    )
    guard.succeed(
        {"purchase_order_ids": [po.id for po in created], "plan_id": plan.id},
        entity_type="replenishment_plan",
        entity_id=plan.id,
    )
    return created


# ---------------------------------------------------------------- 修订替代


def supersede_plan(
    session: Session,
    *,
    plan_id: str,
    actor_id: str,
    requested_window: int | None = None,
    products: list[str] | None = None,
    operation_id: str = "",
) -> ReplenishmentPlan:
    """创建整单修订版并把旧计划标记 superseded（需求 6.1 / 架构 7.1）。"""
    old = session.get(ReplenishmentPlan, plan_id)
    if old is None:
        raise NotFoundError(f"计划不存在 {plan_id}")
    if old.status not in (PLAN_PENDING_APPROVAL, PLAN_APPROVED):
        raise InvalidStateTransitionError(f"只有待审批/已批准计划可以修订，当前状态 {old.status}")
    po_count = session.scalar(select(PurchaseOrder).where(PurchaseOrder.plan_id == plan_id))
    if po_count is not None:
        raise InvalidStateTransitionError("原计划已关联采购单，禁止修订替代")

    guard = IdempotencyGuard(
        session,
        principal_id=actor_id,
        command_type=CMD_SUPERSEDE,
        aggregate_ref=plan_id,
        idempotency_key=operation_id or f"supersede:{plan_id}",
        payload={"plan_id": plan_id, "requested_window": requested_window, "products": products},
    )
    begin = guard.begin()
    if begin.action == "replay_success":
        replayed = session.get(ReplenishmentPlan, begin.response.get("plan_id"))
        assert replayed is not None
        return replayed
    if begin.action == "replay_failure":
        replay_failure(begin)

    warehouse = session.get(Warehouse, old.warehouse_id)
    if warehouse is None:  # pragma: no cover
        raise NotFoundError(f"仓库不存在 {old.warehouse_id}")

    old_lines = session.scalars(select(PlanLine).where(PlanLine.plan_id == plan_id)).all()
    pairs = [(line.warehouse_id, line.product_id) for line in old_lines if line.active_for_dedupe]
    acquire_warehouse_product_locks(session, pairs)
    for line in old_lines:
        if line.active_for_dedupe:
            line.active_for_dedupe = False

    new_window = requested_window or old.requested_window
    new_products = products or [line.product_id for line in old_lines if line.flag == LINE_VALID]
    result = generate_draft(
        session,
        warehouse_id=old.warehouse_id,
        requested_window=new_window,
        actor_id=actor_id,
        products=new_products,
        operation_id=operation_id or f"revision:{plan_id}",
        thread_id=old.thread_id,
        trigger_type="manual",
        use_guard=False,
    )
    if result.plan_id is None:
        raise ValidationError("修订版没有任何有效明细，无法创建")

    new_plan = session.get(ReplenishmentPlan, result.plan_id)
    assert new_plan is not None
    new_plan.revision_of_plan_id = plan_id
    old.status = PLAN_SUPERSEDED
    old.version += 1
    write_audit(
        session,
        actor_id=actor_id,
        action=CMD_SUPERSEDE,
        entity_type="replenishment_plan",
        entity_id=old.id,
        before_state={"status": old.status},
        after_state={"revision_plan_id": new_plan.id},
    )
    guard.succeed(
        {"plan_id": new_plan.id, "revision_of_plan_id": plan_id},
        entity_type="replenishment_plan",
        entity_id=old.id,
    )
    return new_plan
