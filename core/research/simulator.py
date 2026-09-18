"""保守的 K 线挂单仿真器。

快速筛选阶段同时评估 OHLC 与 OLHC 两种路径，取权益更低的结果，避免只选择有利盘中路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .metrics import calculate_metrics
from .models import BacktestResult, Bar, CostModel, Trade


@dataclass(frozen=True, slots=True)
class SimOrder:
    side: str
    price: float
    volume: float
    take_profit: float | None = None
    stop_loss: float | None = None


class BarBacktester:
    """事件函数在每根已闭合 K 线后提交下一根可成交的挂单。"""

    def __init__(self, cost_model: CostModel, *, initial_equity: float = 100_000.0) -> None:
        self._cost = cost_model
        self._initial_equity = float(initial_equity)

    def run(
        self,
        bars: Sequence[Bar],
        decide: Callable[[Bar], Iterable[SimOrder]],
    ) -> BacktestResult:
        # 每根闭合 K 线只计算一次决策，再复用到两种盘中路径，避免策略状态导致路径间泄漏。
        decisions = tuple(tuple(decide(bar)) for bar in bars)
        candidates = [
            self._run_path(bars, decisions, high_first=True),
            self._run_path(bars, decisions, high_first=False),
        ]
        # 以最终权益较低的路径作为快速回测结果，降低乐观偏差。
        return min(candidates, key=lambda item: item.equity_curve[-1] if item.equity_curve else self._initial_equity)

    def _run_path(
        self,
        bars: Sequence[Bar],
        decisions: Sequence[Sequence[SimOrder]],
        *,
        high_first: bool,
    ) -> BacktestResult:
        pending: list[SimOrder] = []
        equity = self._initial_equity
        curve = [equity]
        trades: list[Trade] = []
        for index, bar in enumerate(bars):
            path = (bar.open, bar.high, bar.low, bar.close) if high_first else (bar.open, bar.low, bar.high, bar.close)
            filled: list[SimOrder] = []
            for order in pending:
                if self._crosses(path, order.price):
                    filled.append(order)
            pending = [order for order in pending if order not in filled]
            for order in filled:
                exit_price = self._resolve_exit(order, path)
                direction = 1.0 if order.side == "buy" else -1.0
                gross = (exit_price - order.price) * direction * order.volume
                cost = self._cost.transaction_cost(order.volume) * 2.0 + bar.spread * order.volume
                trade = Trade(bar.timestamp, bar.timestamp, order.side, order.volume, order.price, exit_price, gross, cost)
                trades.append(trade)
                equity += trade.net_pnl
            pending.extend(order for order in decisions[index] if self._fillable_volume(order.volume, bar.volume) > 0)
            curve.append(equity)
        metrics = calculate_metrics(curve, trades)
        return BacktestResult(tuple(curve), tuple(trades), metrics, {"path": "OHLC" if high_first else "OLHC"})

    def _fillable_volume(self, volume: float, bar_volume: float) -> float:
        if volume < self._cost.minimum_fill_volume:
            return 0.0
        if bar_volume > 0 and volume > bar_volume * self._cost.max_volume_share:
            return 0.0
        return volume

    @staticmethod
    def _crosses(path: tuple[float, ...], price: float) -> bool:
        return any(min(left, right) <= price <= max(left, right) for left, right in zip(path, path[1:]))

    @staticmethod
    def _resolve_exit(order: SimOrder, path: tuple[float, ...]) -> float:
        targets = [value for value in (order.take_profit, order.stop_loss) if value is not None]
        for left, right in zip(path, path[1:]):
            crossed = [target for target in targets if min(left, right) <= target <= max(left, right)]
            if crossed:
                # 同一段同时触发止盈/止损时按最不利退出，避免 K 线回测乐观偏差。
                if order.stop_loss in crossed:
                    return float(order.stop_loss)
                return float(crossed[0])
        return path[-1]
