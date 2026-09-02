"""结构化规则解析与适用性校验（需求 4.3 / 架构 5 / ADR 决策 2）。

RAG 只产生候选来源；本服务按来源、版本、生效时间、仓库和 SKU 校验适用性。
优先级：SKU > 分类 > 仓库 > 全局；同级按"已启用、当前有效、版本号最高"选择；
仍冲突则阻断相关明细，不猜测。只有本服务输出的已校验字段可以进入计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import RULE_SAFETY_STOCK, RULE_SCOPE_ORDER, RULE_TYPES
from app.models.replenishment import ReplenishmentRule


@dataclass
class RuleResolution:
    rule: ReplenishmentRule | None
    blocked: bool = False
    block_code: str | None = None
    reason: str | None = None


def _rule_covers(rule: ReplenishmentRule, business_date: date) -> bool:
    if not rule.enabled:
        return False
    if business_date < rule.effective_from:
        return False
    return not (rule.effective_to is not None and business_date > rule.effective_to)


def resolve_rule(
    session: Session,
    *,
    warehouse_id: str,
    product_id: str,
    category: str,
    business_date: date,
    rule_type: str,
) -> RuleResolution:
    """按优先级解析唯一适用规则；同级冲突返回 blocked。"""
    if rule_type not in RULE_TYPES:
        return RuleResolution(None, blocked=True, block_code="invalid_rule_type", reason=f"未知规则类型 {rule_type}")

    for scope in RULE_SCOPE_ORDER:
        stmt = select(ReplenishmentRule).where(  # noqa: E501
            ReplenishmentRule.rule_type == rule_type,
            ReplenishmentRule.scope == scope,
        )
        if scope == "product":
            stmt = stmt.where(ReplenishmentRule.scope_product_id == product_id)
        elif scope == "category":
            stmt = stmt.where(ReplenishmentRule.scope_category == category)
        elif scope == "warehouse":
            stmt = stmt.where(ReplenishmentRule.scope_warehouse_id == warehouse_id)
        candidates = [r for r in session.scalars(stmt).all() if _rule_covers(r, business_date)]
        if not candidates:
            continue
        max_version = max(r.version for r in candidates)
        winners = [r for r in candidates if r.version == max_version]
        if len(winners) == 1:
            return RuleResolution(rule=winners[0])
        return RuleResolution(
            None,
            blocked=True,
            block_code="rule_conflict",
            reason=(f"规则类型 {rule_type} 在作用域 {scope} 同级冲突（版本 {max_version} 存在多条），阻断"),
        )
    return RuleResolution(rule=None)


def resolve_safety_stock_rule(
    session: Session,
    *,
    warehouse_id: str,
    product_id: str,
    category: str,
    business_date: date,
) -> RuleResolution:
    """安全库存规则（V1 固定安全库存来源）。缺失或冲突时调用方阻断明细。"""
    return resolve_rule(
        session,
        warehouse_id=warehouse_id,
        product_id=product_id,
        category=category,
        business_date=business_date,
        rule_type=RULE_SAFETY_STOCK,
    )
