"""确定性预测服务（需求 5.1 / 架构 6.1 / ADR 决策 3）。

- 基线一 wma_28_v1：最近 28 个完整日的加权移动平均，权重从最早日的 1 递增到最近日的 28；
- 基线二 ses_a03_v1：同一 28 日窗口、固定 alpha=0.3 的简单指数平滑，以窗口最早日需求为初值；
- 最近 28 个完整日做滚动单步回测；每个验证日只使用其此前 28 日，至少需要 56 个连续完整日；
- 选择顺序：(WAPE, MAE, algorithm_id)；验证期实际需求总和为 0 时 WAPE 记为 null，按 (MAE, algorithm_id)；  # noqa: E501
- 数据不足/覆盖不可信时降级 fixed_safety_stock_fallback，不伪造 0 预测。

本服务为纯函数（不访问数据库），全部使用 Decimal（十进制有效精度 28），
持久化值为 ROUND_HALF_UP 保留 12 位。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, getcontext

from app.constants import (
    ALGO_ORDER,
    ALGO_SES,
    ALGO_WMA,
    FALLBACK_FIXED_SAFETY_STOCK,
)

getcontext().prec = 28

_ALPHA = Decimal("0.3")
_WEIGHT_SUM = Decimal(sum(range(1, 29)))  # 1+...+28 = 406
_WINDOW = 28


@dataclass(frozen=True)
class DemandDay:
    """一个业务日的需求事实。source_complete=False 表示来源缺失，不得当作 0。"""

    day: date
    demand: int
    source_complete: bool = True


@dataclass
class ForecastResult:
    daily_forecast: Decimal | None
    algorithm: str | None
    mae: Decimal | None
    wape: Decimal | None
    fallback: bool
    fallback_reason: str | None = None
    validation_days: int = 0
    train_days: int = 0
    validation_sum: Decimal = Decimal("0")
    algorithm_version: str = "forecast-v1"


def _wma_forecast(demands: list[int]) -> Decimal:
    """最近 28 个完整日加权移动平均：权重 1..28，日均预测 = 加权和 / (1+...+28)。"""
    total = Decimal(0)
    for idx, demand in enumerate(demands, start=1):  # 最早日权重 1，最近日权重 28
        total += Decimal(demand) * Decimal(idx)
    return total / _WEIGHT_SUM


def _ses_forecast(demands: list[int]) -> Decimal:
    """同一 28 日窗口的简单指数平滑：S_0 = 最早日需求，S_t = alpha*y_t + (1-alpha)*S_{t-1}。"""
    state = Decimal(demands[0])
    for demand in demands[1:]:
        state = _ALPHA * Decimal(demand) + (Decimal(1) - _ALPHA) * state
    return state


def _round12(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1e-12"), rounding=ROUND_HALF_UP)


def _extract_consecutive_complete_days(
    series: list[DemandDay],
) -> list[DemandDay]:
    """取序列末尾最长的一段"连续完整日"（日历日连续且 source_complete=True）。

    来源缺失的日期不能当作 0：一旦出现不完整或断裂，就截断到该日之前。
    """
    if not series:
        return []
    consecutive: list[DemandDay] = []
    prev: date | None = None
    for item in series:
        if not item.source_complete:
            break
        if prev is not None and (item.day - prev).days != 1:
            break
        consecutive.append(item)
        prev = item.day
    return consecutive


def _run_backtest(
    consecutive: list[DemandDay],
) -> tuple[dict[str, tuple[Decimal, Decimal | None]], Decimal]:
    """对最后 28 个完整日做滚动单步回测。

    返回 {algorithm_id: (mae, wape)} 与验证期实际需求总和。每个验证日使用其此前 28 日。
    """
    n = len(consecutive)
    metrics: dict[str, tuple[Decimal, Decimal | None]] = {
        ALGO_WMA: (Decimal(0), None),
        ALGO_SES: (Decimal(0), None),
    }
    validation_sum = Decimal(0)
    valid_days = 0
    for i in range(n - _WINDOW, n):  # 最后 28 日
        train = [d.demand for d in consecutive[i - _WINDOW : i]]
        actual = consecutive[i].demand
        validation_sum += Decimal(actual)
        valid_days += 1
        for algo, forecast_fn in ((ALGO_WMA, _wma_forecast), (ALGO_SES, _ses_forecast)):
            forecast = forecast_fn(train)
            err = abs(forecast - Decimal(actual))
            mae, _ = metrics[algo]
            metrics[algo] = (mae + err, None)
    if valid_days == 0:
        return metrics, validation_sum
    total_abs = Decimal(0)
    for i in range(n - _WINDOW, n):
        train = [d.demand for d in consecutive[i - _WINDOW : i]]
        actual = consecutive[i].demand
        for _algo, forecast_fn in ((ALGO_WMA, _wma_forecast), (ALGO_SES, _ses_forecast)):
            forecast = forecast_fn(train)
            total_abs += abs(forecast - Decimal(actual))
    for algo in (ALGO_WMA, ALGO_SES):
        mae, _ = metrics[algo]
        metrics[algo] = (
            mae / Decimal(valid_days),
            (total_abs / validation_sum) if validation_sum > 0 else None,
        )
    return metrics, validation_sum


def _select_algorithm(metrics: dict[str, tuple[Decimal, Decimal | None]], validation_sum: Decimal) -> str:
    def key(algo: str) -> tuple[Decimal, Decimal, int]:
        mae, wape = metrics[algo]
        # 总和为 0：WAPE 记为 null，不参与比较
        w = (wape if wape is not None else Decimal("Infinity")) if validation_sum > 0 else Decimal(0)
        return w, mae, ALGO_ORDER.index(algo)

    return min((ALGO_WMA, ALGO_SES), key=key)


def forecast_daily_demand(
    series: list[DemandDay],
    *,
    coverage_untrustworthy: bool = False,
) -> ForecastResult:
    """对"预测日"（最后一个完整日的次日）生成日需求预测。

    coverage_untrustworthy=True 表示数据覆盖状态不可信，直接降级。
    """
    if coverage_untrustworthy:
        return ForecastResult(
            daily_forecast=None,
            algorithm=None,
            mae=None,
            wape=None,
            fallback=True,
            fallback_reason=FALLBACK_FIXED_SAFETY_STOCK,
        )

    consecutive = _extract_consecutive_complete_days(series)
    if len(consecutive) < 2 * _WINDOW:
        return ForecastResult(
            daily_forecast=None,
            algorithm=None,
            mae=None,
            wape=None,
            fallback=True,
            fallback_reason=FALLBACK_FIXED_SAFETY_STOCK,
            validation_days=0,
            train_days=len(consecutive),
        )

    metrics, validation_sum = _run_backtest(consecutive)
    chosen = _select_algorithm(metrics, validation_sum)
    mae, wape = metrics[chosen]

    # 用最近 28 个完整日生成预测日（次日）的预测
    recent_train = [d.demand for d in consecutive[-_WINDOW:]]
    forecast_fn = _wma_forecast if chosen == ALGO_WMA else _ses_forecast
    daily = _round12(forecast_fn(recent_train))

    return ForecastResult(
        daily_forecast=daily,
        algorithm=chosen,
        mae=_round12(mae),
        wape=_round12(wape) if wape is not None else None,
        fallback=False,
        validation_days=_WINDOW,
        train_days=len(consecutive),
        validation_sum=_round12(validation_sum),
    )
