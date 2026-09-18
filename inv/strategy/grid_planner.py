"""策略 4：网格规划器

基于经典网格交易法，在价格区间内设置多层买入/卖出挂单。
适用于震荡行情，通过低买高卖积累利润。

参考：
  "Grid Trading: The Automatic Trading System" - 多种来源的经典网格策略

核心逻辑：
- 在预设的价格区间 [min_price, max_price] 内划分 N 层网格
- 每层设置买入和卖出挂单
- 价格上涨时卖出，价格下跌时买入
- 自动调整网格层数以控制风险敞口
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from inv.strategy.base import BaseStrategy
from inv.domain.models import Bar, Signal, SignalType
from inv.domain.settings import StrategySettings


class GridPlanner(BaseStrategy):
    """网格策略规划器。"""

    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        if current_index < 1:
            return self._hold_signal(bars, current_index)

        closes = [float(b.close) for b in bars[: current_index + 1]]
        current_price = closes[-1]
        timestamp = self._get_timestamp(bars, current_index)

        min_price = settings.min_price
        max_price = settings.max_price
        grid_levels = settings.window if settings.window > 0 else 6
        tp_dist = settings.tp_dist if settings.tp_dist > 0 else 0.001
        sl_dist = settings.sl_dist if settings.sl_dist > 0 else 0.002

        # 计算网格层
        price_range = max(max_price - min_price, 0.001)
        grid_step = price_range / grid_levels

        # 计算当前价格所处的网格位置
        grid_position = int((current_price - min_price) / grid_step)
        grid_position = max(0, min(grid_position, grid_levels - 1))

        # 网格中线价格
        grid_center = min_price + (grid_position + 0.5) * grid_step

        # 信号逻辑
        has_long = self.state.position > 0
        has_short = self.state.position < 0

        # 价格低于网格中心一定比例 → 买入
        buy_threshold = grid_center * (1 - tp_dist * 2)
        # 价格高于网格中心一定比例 → 卖出
        sell_threshold = grid_center * (1 + tp_dist * 2)

        signals: list[Signal] = []

        if current_price <= buy_threshold and not has_long:
            # 计算 ATR 用于动态网格间距
            atr = self._calc_atr(bars, current_index, settings.atr_period)
            sl_price = current_price * (1 - sl_dist * 3)
            tp_price = grid_center

            confidence = max(0.3, min(1.0, (buy_threshold - current_price) / (buy_threshold * 0.01 + 1e-12)))
            signals.append(Signal(
                timestamp=timestamp, symbol=self.symbol,
                signal_type=SignalType.BUY, price=current_price,
                stop_loss=sl_price, take_profit=tp_price,
                confidence=min(1.0, confidence),
                metadata={
                    "strategy": "grid", "grid_level": grid_position,
                    "grid_center": grid_center, "grid_step": grid_step,
                    "atr": atr or 0.0,
                },
            ))

        elif current_price >= sell_threshold and not has_short:
            sl_price = current_price * (1 + sl_dist * 3)
            tp_price = grid_center

            confidence = max(0.3, min(1.0, (current_price - sell_threshold) / (sell_threshold * 0.01 + 1e-12)))
            signals.append(Signal(
                timestamp=timestamp, symbol=self.symbol,
                signal_type=SignalType.SELL, price=current_price,
                stop_loss=sl_price, take_profit=tp_price,
                confidence=min(1.0, confidence),
                metadata={
                    "strategy": "grid", "grid_level": grid_position,
                    "grid_center": grid_center, "grid_step": grid_step,
                },
            ))

        # 平仓逻辑：价格回到网格中心
        if has_long and current_price >= grid_center:
            signals.append(Signal(
                timestamp=timestamp, symbol=self.symbol,
                signal_type=SignalType.CLOSE_LONG,
                metadata={"strategy": "grid", "reason": "网格中轨平多"},
            ))
        elif has_short and current_price <= grid_center:
            signals.append(Signal(
                timestamp=timestamp, symbol=self.symbol,
                signal_type=SignalType.CLOSE_SHORT,
                metadata={"strategy": "grid", "reason": "网格中轨平空"},
            ))

        return signals if signals else self._hold_signal(bars, current_index)

    def _calc_atr(
        self, bars: Sequence[Bar], current_index: int, period: int
    ) -> float | None:
        if current_index < period + 1:
            return None
        start = max(0, current_index - period - 1)
        tail = bars[start : current_index + 1]
        if len(tail) < period + 1:
            return None
        true_ranges = []
        for prev, curr in zip(tail, tail[1:]):
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
            true_ranges.append(tr)
        return sum(true_ranges[-period:]) / period if true_ranges else None

    def _hold_signal(
        self, bars: Sequence[Bar], current_index: int
    ) -> list[Signal]:
        return [
            Signal(
                timestamp=self._get_timestamp(bars, current_index),
                symbol=self.symbol,
                signal_type=SignalType.HOLD,
                metadata={"strategy": "grid"},
            )
        ]

    @staticmethod
    def _get_timestamp(bars: Sequence[Bar], current_index: int) -> datetime:
        ts = bars[min(current_index, len(bars) - 1)].timestamp
        if isinstance(ts, datetime):
            return ts
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)

    def reset_state(self) -> None:
        super().reset_state()


__all__ = ["GridPlanner"]