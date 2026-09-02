"""确定性补货量计算服务（需求 5.3 / 架构 6.3 / ADR 决策 3）。

```text
available = on_hand - reserved
planning_window = max(requested_window, lead_days + review_period_days)
coverage_demand = daily_forecast * planning_window
target_stock = coverage_demand + fixed_safety_stock
inbound = po_created/ordering/ordered/order_unknown/partially_received 的剩余未收量
net_demand = max(0, target_stock - available - inbound)
order_qty = 0                                        if net_demand == 0
order_qty = ceil(max(net_demand, minimum_order_qty) / pack_multiple) * pack_multiple  if net_demand > 0  # noqa: E501
```

要求：全部数量/交期/规则值合法非负、reserved <= on_hand、pack_multiple > 0、
不超过业务数量上限；非法输入抛 BlockedInputError，不得静默修正。
中间过程不提前取整；持久化值 ROUND_HALF_UP 保留 12 位；最终订货量只按 ceil 公式取整。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, getcontext

from app.errors import BlockedInputError

getcontext().prec = 28

# 业务数量上限（需求 5.3：超过业务数量上限必须拒绝，不静默修正）
MAX_ORDER_QTY = Decimal("1000000000")  # 10^9
MAX_ON_HAND = Decimal("1000000000000")  # 10^12

_SCALE12 = Decimal("1e-12")


def _q12(value: Decimal) -> Decimal:
    return value.quantize(_SCALE12, rounding=ROUND_HALF_UP)


@dataclass
class ReplenishmentInput:
    requested_window: int
    daily_forecast: Decimal | None  # None => 降级模式 coverage_demand=0
    fixed_safety_stock: Decimal
    lead_days: int
    review_period_days: int
    minimum_order_qty: int
    pack_multiple: int
    on_hand: int
    reserved: int
    inbound: Decimal  # 有效在途剩余量（由采购域统计，调用方传入）


@dataclass
class ReplenishmentResult:
    available: Decimal
    planning_window: int
    coverage_demand: Decimal
    target_stock: Decimal
    inbound: Decimal
    net_demand: Decimal
    order_qty: int
    fallback: bool = False  # daily_forecast 为 None（fixed_safety_stock_fallback）


def _check_quantity(name: str, value: object, *, upper: Decimal) -> Decimal:
    dec = Decimal(str(value))
    if dec.is_nan() or dec.is_infinite() or dec < 0:
        raise BlockedInputError(f"非法数量 {name}={value}：必须为非负合法值")
    if dec > upper:
        raise BlockedInputError(f"数量 {name}={value} 超过业务上限 {upper}，拒绝而非修正")
    return dec


def compute_replenishment(inp: ReplenishmentInput) -> ReplenishmentResult:
    on_hand = _check_quantity("on_hand", inp.on_hand, upper=MAX_ON_HAND)
    reserved = _check_quantity("reserved", inp.reserved, upper=MAX_ON_HAND)
    if reserved > on_hand:
        raise BlockedInputError(f"reserved({reserved}) > on_hand({on_hand})，非法输入，阻断")
    if inp.lead_days < 0:
        raise BlockedInputError(f"lead_days({inp.lead_days}) 必须非负")
    if inp.review_period_days < 0:
        raise BlockedInputError(f"review_period_days({inp.review_period_days}) 必须非负")
    if inp.minimum_order_qty < 0:
        raise BlockedInputError(f"minimum_order_qty({inp.minimum_order_qty}) 必须非负")
    if inp.pack_multiple <= 0:
        raise BlockedInputError(f"pack_multiple({inp.pack_multiple}) 必须大于 0")
    if inp.requested_window not in (7, 14, 30):
        raise BlockedInputError(f"requested_window({inp.requested_window}) 只允许 7/14/30")
    if inp.inbound.is_nan() or inp.inbound.is_infinite() or inp.inbound < 0:
        raise BlockedInputError(f"inbound({inp.inbound}) 必须为非负合法值")
    safety = _check_quantity("fixed_safety_stock", inp.fixed_safety_stock, upper=MAX_ON_HAND)

    available = on_hand - reserved
    planning_window = max(inp.requested_window, inp.lead_days + inp.review_period_days)

    fallback = inp.daily_forecast is None
    if fallback:
        coverage_demand = Decimal(0)
    else:
        forecast = _check_quantity("daily_forecast", inp.daily_forecast, upper=MAX_ON_HAND)
        coverage_demand = forecast * Decimal(planning_window)

    target_stock = coverage_demand + safety
    net_demand = max(Decimal(0), target_stock - available - inp.inbound)

    if net_demand == 0:
        order_qty = 0
    else:
        orderable = max(net_demand, Decimal(inp.minimum_order_qty))
        ceil_units = (orderable / Decimal(inp.pack_multiple)).to_integral_value(rounding=ROUND_CEILING)
        order_qty = int(ceil_units * Decimal(inp.pack_multiple))
        if order_qty > int(MAX_ORDER_QTY):
            raise BlockedInputError(f"订货量 {order_qty} 超过业务上限，阻断")

    return ReplenishmentResult(
        available=_q12(available),
        planning_window=planning_window,
        coverage_demand=_q12(coverage_demand),
        target_stock=_q12(target_stock),
        inbound=_q12(inp.inbound),
        net_demand=_q12(net_demand),
        order_qty=order_qty,
        fallback=fallback,
    )
