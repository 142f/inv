"""走样本验证、Pareto 前沿与稳健候选筛选。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from core.domain import PerformanceMetrics


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train: slice
    validate: slice
    test: slice


def walk_forward_splits(length: int, *, train_size: int, validate_size: int, test_size: int, step: int | None = None) -> tuple[WalkForwardSplit, ...]:
    stride = step or test_size
    result: list[WalkForwardSplit] = []
    start = 0
    while start + train_size + validate_size + test_size <= length:
        middle = start + train_size
        end_validate = middle + validate_size
        result.append(WalkForwardSplit(slice(start, middle), slice(middle, end_validate), slice(end_validate, end_validate + test_size)))
        start += stride
    return tuple(result)


def pareto_front(candidates: Mapping[str, PerformanceMetrics]) -> tuple[str, ...]:
    """收益、Sharpe、Sortino、PF 越高越好；回撤、波动、成本与 CVaR 越低越好。"""
    keys = tuple(candidates)
    front: list[str] = []
    for key in keys:
        metric = candidates[key]
        if not any(_dominates(candidates[other], metric) for other in keys if other != key):
            front.append(key)
    return tuple(sorted(front))


def select_robust_candidate(
    candidates: Mapping[str, Sequence[PerformanceMetrics]],
    *,
    max_drawdown: float,
    min_trades: int,
) -> str | None:
    """先硬性过滤，再以最差窗口的风险调整收益做确定性决胜。"""
    eligible: dict[str, Sequence[PerformanceMetrics]] = {
        key: values
        for key, values in candidates.items()
        if values and all(item.max_drawdown <= max_drawdown and item.trade_count >= min_trades for item in values)
    }
    if not eligible:
        return None
    return max(
        sorted(eligible),
        key=lambda key: min(
            item.sortino_ratio + item.sharpe_ratio + item.profit_factor - item.max_drawdown - item.cost_ratio
            for item in eligible[key]
        ),
    )


def _dominates(left: PerformanceMetrics, right: PerformanceMetrics) -> bool:
    higher_or_equal = (
        left.net_profit >= right.net_profit
        and left.sharpe_ratio >= right.sharpe_ratio
        and left.sortino_ratio >= right.sortino_ratio
        and left.profit_factor >= right.profit_factor
    )
    lower_or_equal = (
        left.max_drawdown <= right.max_drawdown
        and left.volatility <= right.volatility
        and left.cost_ratio <= right.cost_ratio
        and left.cvar <= right.cvar
    )
    strictly_better = (
        left.net_profit > right.net_profit
        or left.sharpe_ratio > right.sharpe_ratio
        or left.sortino_ratio > right.sortino_ratio
        or left.profit_factor > right.profit_factor
        or left.max_drawdown < right.max_drawdown
        or left.volatility < right.volatility
        or left.cost_ratio < right.cost_ratio
        or left.cvar < right.cvar
    )
    return higher_or_equal and lower_or_equal and strictly_better
