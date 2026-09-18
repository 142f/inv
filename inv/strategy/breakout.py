"""策略 3：突破策略（唐奇安通道突破）

基于经典的唐奇安通道（Donchian Channel）突破系统。
与海龟交易法则（Turtle Trading System）的核心逻辑一致。

参考：
  "Way of the Turtle" - Curtis Faith (2007)
  "What I Learned Losing a Million Dollars" - Jim Paul, Brendan Moynihan

核心逻辑：
- 计算过去 N 期的最高价和最低价（唐奇安通道）
- 价格突破上轨 → 买入
- 价格跌破下轨 → 卖出
- 使用 ATR 设置止损和止盈
- 价格回到通道内触发平仓
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import numpy as np

from inv.strategy.base import BaseStrategy
from inv.domain.models import Bar, Signal, SignalType
from inv.domain.settings import StrategySettings


class BreakoutStrategy(BaseStrategy):
    """唐奇安通道突破策略。"""

    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        lookback = settings.breakout_lookback
        if current_index < lookback + settings.breakout_confirmation_bars:
            return self._hold_signal(bars, current_index)

        # 仅使用已闭合的 K 线数据
        tail = bars[max(0, current_index - lookback + 1) : current_index + 1]
        if len(tail) < lookback:
            return self._hold_signal(bars, current_index)

        highs = np.array([float(b.high) for b in tail])
        lows = np.array([float(b.low) for b in tail])
        closes = np.array([float(b.close) for b in bars[: current_index + 1]], dtype=np.float64)

        channel_high = float(np.max(highs))
        channel_low = float(np.min(lows))
        channel_mid = (channel_high + channel_low) * 0.5

        current_close = closes[-1]
        timestamp = self._get_timestamp(bars, current_index)
        atr = self._calc_atr(bars, current_index, settings.atr_period)
        atr_value = atr or (channel_high - channel_low) * 0.1

        # 确认突破：连续 confirmation_bars 根收盘价在通道外
        confirmation = settings.breakout_confirmation_bars
        confirmed_breakout_up = self._check_confirmation(
            closes, lookback, confirmation, direction="up"
        )
        confirmed_breakout_down = self._check_confirmation(
            closes, lookback, confirmation, direction="down"
        )

        has_long = self.state.position > 0
        has_short = self.state.position < 0

        # 突破买入
        if confirmed_breakout_up and not has_long:
            sl = current_close - atr_value * settings.breakout_atr_mult
            tp = current_close + atr_value * settings.breakout_atr_mult * 2
            strength = (current_close - channel_high) / (atr_value + 1e-12)
            confidence = min(1.0, max(0.3, strength / 3.0))
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=SignalType.BUY, price=current_close,
                    stop_loss=sl, take_profit=tp,
                    confidence=confidence,
                    metadata={
                        "strategy": "breakout", "channel_high": channel_high,
                        "channel_low": channel_low, "channel_mid": channel_mid,
                        "atr": atr_value, "breakout_strength": strength,
                    },
                )
            ]

        # 突破卖出
        if confirmed_breakout_down and not has_short:
            sl = current_close + atr_value * settings.breakout_atr_mult
            tp = current_close - atr_value * settings.breakout_atr_mult * 2
            strength = (channel_low - current_close) / (atr_value + 1e-12)
            confidence = min(1.0, max(0.3, strength / 3.0))
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=SignalType.SELL, price=current_close,
                    stop_loss=sl, take_profit=tp,
                    confidence=confidence,
                    metadata={
                        "strategy": "breakout", "channel_high": channel_high,
                        "channel_low": channel_low, "channel_mid": channel_mid,
                        "atr": atr_value, "breakout_strength": strength,
                    },
                )
            ]

        # 平仓逻辑：价格回到通道中轨
        signal_type = None
        if has_long:
            if current_close <= channel_mid:
                signal_type = SignalType.CLOSE_LONG
        elif has_short:
            if current_close >= channel_mid:
                signal_type = SignalType.CLOSE_SHORT

        if signal_type:
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=signal_type,
                    metadata={"strategy": "breakout", "reason": "通道中轨回归"},
                )
            ]

        return self._hold_signal(bars, current_index)

    def _check_confirmation(
        self,
        closes: np.ndarray,
        lookback: int,
        confirmation_bars: int,
        direction: str,
    ) -> bool:
        """检查突破确认：连续 N 根收盘价在通道外。"""
        if confirmation_bars <= 1:
            return True
        if len(closes) < lookback + confirmation_bars:
            return False

        channel_slice = closes[-(lookback + confirmation_bars) : -confirmation_bars]
        confirm_slice = closes[-confirmation_bars:]

        channel_high = float(np.max(channel_slice))
        channel_low = float(np.min(channel_slice))

        if direction == "up":
            return all(c > channel_high for c in confirm_slice)
        else:
            return all(c < channel_low for c in confirm_slice)

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
                metadata={"strategy": "breakout"},
            )
        ]

    @staticmethod
    def _get_timestamp(bars: Sequence[Bar], current_index: int) -> datetime:
        ts = bars[min(current_index, len(bars) - 1)].timestamp
        if isinstance(ts, datetime):
            return ts
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)