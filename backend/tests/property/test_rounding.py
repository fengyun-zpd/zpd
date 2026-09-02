"""取整与补货公式性质测试（需求 5.3 / 5.3.1）。"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.replenishment import ReplenishmentInput, compute_replenishment

_quantities = st.integers(min_value=0, max_value=10**6)
_decimal_positive = st.decimals(min_value=0, max_value=10**5, places=4)


@settings(max_examples=200, deadline=None)
@given(
    on_hand=_quantities,
    reserved=_quantities,
    daily=_decimal_positive,
    safety=st.decimals(min_value=0, max_value=10**4, places=2),
    inbound=st.decimals(min_value=0, max_value=10**6, places=2),
    min_qty=_quantities,
    pack=st.integers(min_value=1, max_value=1000),
    lead=st.integers(min_value=0, max_value=60),
    review=st.integers(min_value=0, max_value=30),
    window=st.sampled_from([7, 14, 30]),
)
def test_order_qty_is_pack_multiple(on_hand, reserved, daily, safety, inbound, min_qty, pack, lead, review, window):
    if reserved > on_hand:
        return  # 非法输入由守卫测试覆盖
    inp = ReplenishmentInput(
        requested_window=window,
        daily_forecast=Decimal(str(daily)),
        fixed_safety_stock=Decimal(str(safety)),
        lead_days=lead,
        review_period_days=review,
        minimum_order_qty=min_qty,
        pack_multiple=pack,
        on_hand=on_hand,
        reserved=reserved,
        inbound=Decimal(str(inbound)),
    )
    result = compute_replenishment(inp)
    assert result.net_demand >= 0
    if result.net_demand == 0:
        assert result.order_qty == 0
    else:
        assert result.order_qty % pack == 0
        assert result.order_qty >= pack  # ceil 至少一箱
        # 向上取整保证：结果 >= max(net, min_qty)
        assert result.order_qty >= max(result.net_demand, Decimal(min_qty)) - Decimal("1e-9")


@settings(max_examples=100, deadline=None)
@given(qty=st.decimals(min_value=0, max_value=10**6, places=8))
def test_round12_half_up_scale(qty):
    from app.services.replenishment import _q12

    value = _q12(Decimal(str(qty)))
    exponent = value.as_tuple().exponent
    assert exponent >= -12  # 不超过 12 位小数
    # ROUND_HALF_UP 确定性：同一输入同一输出
    assert _q12(Decimal(str(qty))) == value
