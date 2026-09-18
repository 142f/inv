"""策略 2：均值回归策略

基于布林带（Bollinger Bands）的均值回归策略。
当价格触及下轨时买入，触及上轨时卖出，回到中轨时平仓。

参考：
  "Bollinger on Bollinger Bands" - John Bollinger (2001)
  以及经典的配对交易（Pairs Trading）均值回归思想。

核心逻辑：
- 计算移动平均（MA）和标准差
- 布林带 = MA ± k * 标准差
- 价格触及下轨 → 买入
- 价格触及上轨 → 卖出
- 价格回到中轨 → 平仓
- 极端突破（超过 2 倍带宽）视为趋势开始，不做反向操作
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import numpy as np

from inv.strategy.base import BaseStrategy
from inv.domain.models import Bar, Signal, SignalType
from inv.domain.settings import StrategySettings


class MeanReversionStrategy(BaseStrategy):
    """布林带均值回归策略。"""

    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        lookback = settings.lookback_period
        if current_index < lookback:
            return self._hold_signal(bars, current_index)

        closes = np.array([float(b.close) for b in bars[: current_index + 1]], dtype=np.float64)
        if len(closes) < lookback + 1:
            return self._hold_signal(bars, current_index)

        # 封装布林带相关的统计计算
        ma, std = self._bollinger_stats(closes, lookback)
        if std < 1e-12:
            return self._hold_signal(bars, current_index)

        current_close = closes[-1]
        timestamp = self._get_timestamp(bars, current_index)

        # 使用 entry_threshold 作为标准差倍数（常规布林带使用 2）
        k_entry = settings.entry_threshold  # 入场带宽倍数
        k_exit = settings.exit_threshold  # 出场带宽倍数（应小于 k_entry）

        upper_entry = ma + k_entry * std
        lower_entry = ma - k_entry * std
        upper_exit = ma + k_exit * std
        lower_exit = ma - k_exit * std

        has_long = self.state.position > 0
        has_short = self.state.position < 0

        # 计算当前价格在布林带中的相对位置
        bb_position = (current_close - ma) / (std + 1e-12)

        # 极端趋势检测：价格突破 3 倍标准差，视为趋势而非均值回归机会
        extreme_threshold = max(2.5, k_entry * 1.5)
        is_extreme = abs(bb_position) > extreme_threshold

        if is_extreme:
            # 极端行情，跟随趋势
            if bb_position > extreme_threshold and not has_long:
                atr = self._calc_atr(bars, current_index, settings.atr_period)
                sl = current_close - (atr or std) * settings.stop_loss_atr
                tp = current_close + (atr or std) * settings.take_profit_atr
                return [
                    Signal(
                        timestamp=timestamp, symbol=self.symbol,
                        signal_type=SignalType.BUY, price=current_close,
                        stop_loss=sl, take_profit=tp,
                        confidence=min(1.0, abs(bb_position) / (extreme_threshold * 2)),
                        metadata={"strategy": "mean_reversion", "mode": "trend_follow",
                                   "bb_position": bb_position, "ma": ma, "std": std},
                    )
                ]
            elif bb_position < -extreme_threshold and not has_short:
                atr = self._calc_atr(bars, current_index, settings.atr_period)
                sl = current_close + (atr or std) * settings.stop_loss_atr
                tp = current_close - (atr or std) * settings.take_profit_atr
                return [
                    Signal(
                        timestamp=timestamp, symbol=self.symbol,
                        signal_type=SignalType.SELL, price=current_close,
                        stop_loss=sl, take_profit=tp,
                        confidence=min(1.0, abs(bb_position) / (extreme_threshold * 2)),
                        metadata={"strategy": "mean_reversion", "mode": "trend_follow",
                                   "bb_position": bb_position, "ma": ma, "std": std},
                    )
                ]

        # 标准均值回归逻辑
        if current_close <= lower_entry and not has_long:
            # 价格触及下轨 → 买入
            atr = self._calc_atr(bars, current_index, settings.atr_period)
            sl = current_close - (atr or std) * settings.stop_loss_atr
            tp = ma  # 目标回到中轨
            confidence = min(1.0, max(0.3, abs(bb_position) / k_entry))
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=SignalType.BUY, price=current_close,
                    stop_loss=sl, take_profit=tp,
                    confidence=confidence,
                    metadata={"strategy": "mean_reversion", "mode": "reversion",
                               "bb_position": bb_position, "ma": ma, "std": std},
                )
            ]

        elif current_close >= upper_entry and not has_short:
            # 价格触及上轨 → 卖出
            atr = self._calc_atr(bars, current_index, settings.atr_period)
            sl = current_close + (atr or std) * settings.stop_loss_atr
            tp = ma  # 目标回到中轨
            confidence = min(1.0, max(0.3, abs(bb_position) / k_entry))
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=SignalType.SELL, price=current_close,
                    stop_loss=sl, take_profit=tp,
                    confidence=confidence,
                    metadata={"strategy": "mean_reversion", "mode": "reversion",
                               "bb_position": bb_position, "ma": ma, "std": std},
                )
            ]

        # 平仓逻辑：价格回到中轨附近
        if has_long and current_close >= upper_exit:
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=SignalType.CLOSE_LONG,
                    metadata={"strategy": "mean_reversion", "reason": "价格回到中轨"},
                )
            ]
        if has_short and current_close <= lower_exit:
            return [
                Signal(
                    timestamp=timestamp, symbol=self.symbol,
                    signal_type=SignalType.CLOSE_SHORT,
                    metadata={"strategy": "mean_reversion", "reason": "价格回到中轨"},
                )
            ]

        return self._hold_signal(bars, current_index)

    @staticmethod
    def _bollinger_stats(closes: np.ndarray, period: int) -> tuple[float, float]:
        """计算移动平均和标准差。"""
        tail = closes[-period:]
        ma = float(np.mean(tail))
        std = float(np.std(tail, ddof=1))
        return ma, std

    def _calc_atr(
        self, bars: Sequence[Bar], current_index: int, period: int
    ) -> float | None:
        """计算 ATR，仅使用已闭合数据。"""
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
        if not true_ranges:
            return None
        return sum(true_ranges[-period:]) / period

    def _hold_signal(
        self, bars: Sequence[Bar], current_index: int
    ) -> list[Signal]:
        return [
            Signal(
                timestamp=self._get_timestamp(bars, current_index),
                symbol=self.symbol,
                signal_type=SignalType.HOLD,
                metadata={"strategy": "mean_reversion"},
            )
        ]

    @staticmethod
    def _get_timestamp(bars: Sequence[Bar], current_index: int) -> datetime:
        ts = bars[min(current_index, len(bars) - 1)].timestamp
        if isinstance(ts, datetime):
            return ts
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)