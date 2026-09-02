"""确定性供应商选择服务（需求 5.2 / 架构 6.2 / ADR 决策 3）。

先排除禁用、超出适用期、不可供货及缺少有效价格/交期/最小量/整箱倍数的关系，
再按"业务优先级升序 -> 交期升序 -> 采购价升序 -> supplier_id 字典序升序"稳定排序。
供应商必须在补货量计算前选定。V1 种子采购价统一为 CNY，不跨币种比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class SupplierCandidate:
    supplier_id: str
    supplier_name: str = ""
    business_priority: int = 100  # 关系级业务优先级（越小越优先）
    lead_days: int = 0
    price: Decimal | None = None  # 采购价（V1 统一 CNY）
    minimum_order_qty: int = 0
    pack_multiple: int = 1
    enabled: bool = True
    effective_from: date | None = None
    effective_to: date | None = None
    currency: str = "CNY"
    raw: dict = field(default_factory=dict)


def select_supplier(
    candidates: list[SupplierCandidate],
    business_date: date,
) -> tuple[SupplierCandidate | None, list[SupplierCandidate], str]:
    """选择供应商。

    返回 (选中关系, 过滤后的候选列表, 选择理由)。无候选时选中关系为 None，
    由调用方将明细标记为 blocked。
    """
    if not candidates:
        return None, [], "无候选供应商关系"

    filtered: list[SupplierCandidate] = []
    for cand in candidates:
        if not cand.enabled:
            continue
        if cand.effective_from is not None and business_date < cand.effective_from:
            continue
        if cand.effective_to is not None and business_date > cand.effective_to:
            continue
        if cand.minimum_order_qty < 0 or cand.lead_days < 0 or cand.pack_multiple <= 0:
            continue
        if cand.price is None or cand.price < 0:
            continue
        filtered.append(cand)

    if not filtered:
        return None, [], "所有候选关系均被过滤（禁用/超出适用期/数据非法）"

    ordered = sorted(
        filtered,
        key=lambda c: (
            c.business_priority,
            c.lead_days,
            str(c.price),
            c.supplier_id,  # 字典序升序，稳定兜底
        ),
    )
    chosen = ordered[0]
    reason = (
        f"按业务优先级->交期->采购价->supplier_id 稳定排序选择 {chosen.supplier_id} "
        f"(priority={chosen.business_priority}, lead={chosen.lead_days}, price={chosen.price})"
    )
    return chosen, ordered, reason
