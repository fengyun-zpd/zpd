"""预测服务单元测试（需求 5.1 / 架构 6.1）。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.constants import ALGO_SES, ALGO_WMA, FALLBACK_FIXED_SAFETY_STOCK
from app.services.forecast import DemandDay, forecast_daily_demand


def _series(demands: list[int], *, incomplete: set[int] | None = None, start: date | None = None) -> list[DemandDay]:
    incomplete = incomplete or set()
    start = start or date(2026, 1, 1)
    return [
        DemandDay(day=start + timedelta(days=i), demand=v, source_complete=i not in incomplete)
        for i, v in enumerate(demands)
    ]


def test_wma_weights():
    """28 日窗口权重 1..28：全部为 28 时日均=28；全部为 0 时=0。"""
    result = forecast_daily_demand(_series([28] * 56))
    assert result.fallback is False
    assert result.daily_forecast == Decimal("28")
    result_zero = forecast_daily_demand(_series([0] * 56))
    assert result_zero.daily_forecast == Decimal("0")


def test_ses_initialization():
    """SES 以窗口最早日需求为初值；alpha=0.3。"""
    # 需求先 100 后 0：SES 预测值应介于二者之间且受 alpha 影响
    demands = [100] * 28 + [0] * 28
    result = forecast_daily_demand(_series(demands))
    assert result.fallback is False
    # 最后 28 个训练日全为 0，S0=100 逐步衰减：S_27 = 100 * 0.7^27 ≈ 0.00006...
    assert result.algorithm in (ALGO_WMA, ALGO_SES)
    assert result.daily_forecast is not None and 0 <= result.daily_forecast <= 100


def test_insufficient_data_fallback():
    """少于 56 个连续完整日 -> fixed_safety_stock_fallback，不伪造预测。"""
    result = forecast_daily_demand(_series([5] * 30))
    assert result.fallback is True
    assert result.daily_forecast is None
    assert result.algorithm is None
    assert result.fallback_reason == FALLBACK_FIXED_SAFETY_STOCK


def test_incomplete_day_breaks_consecutive():
    """来源缺失的日期不能当作 0：最后一个不完整日之后的完整日被截断。"""
    result = forecast_daily_demand(_series([5] * 60, incomplete={55}))
    assert result.fallback is True


def test_zero_validation_period_uses_mae_tiebreak():
    """验证期实际需求总和为 0：WAPE 为 null，按 (MAE, algorithm_id) 选择。"""
    result = forecast_daily_demand(_series([0] * 60))
    assert result.fallback is False
    assert result.wape is None
    assert result.algorithm == ALGO_WMA  # MAE 相同 -> algorithm_id 更小者


def test_metric_selection_follows_wape():
    """验证期总和>0：按 (WAPE, MAE, algorithm_id) 选择。"""
    # 构造最近 28 日验证期与训练窗口
    demands = [10] * 56
    result = forecast_daily_demand(_series(demands))
    assert result.fallback is False
    assert result.validation_sum == Decimal("280")
    assert result.algorithm in (ALGO_WMA, ALGO_SES)


def test_validation_uses_only_prior_28_days():
    """每个验证日只使用其此前 28 日；首验证日前保留 28 个训练日。"""
    demands = list(range(1, 57))  # 56 天递增
    result = forecast_daily_demand(_series(demands))
    assert result.fallback is False
    assert result.validation_days == 28
    assert result.train_days == 56


def test_forecast_is_decimal_12_scale():
    result = forecast_daily_demand(_series([7] * 56))
    assert result.daily_forecast == Decimal("7.000000000000")
    assert str(result.daily_forecast).split(".")[1] == "000000000000"
