"""策略 1：动量策略（趋势跟踪）

基于经典时间序列动量策略（Moskowitz, Ooi, Pedersen 2012）。
使用过去 N 期的收益率作为动量信号，结合 ATR 进行仓位管理和止损。

信号逻辑：
- 计算过去 lookback_period 期的总收益率
- 收益率 > entry_threshold * 收益率标准差 → 买入信号
- 收益率 < -entry_threshold * 收益率标准差 → 卖出信号
- 收益率绝对值 < exit_threshold * 收益率标准差 → 平仓信号

参考论文：
  "Time Series Momentum" - Moskowitz, Ooi, Pedersen (2012)
  Journal of Financial Economics, Vol. 104, No. 2
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from inv.strategy.base import BaseStrategy, StrategyState
from inv.domain.models import Bar, Signal, SignalType
from inv.domain.settings import StrategySettings


class MomentumStrategy(BaseStrategy):
    """时间序列动量策略。

    在回调/回测中的使用:
        strategy = MomentumStrategy("momentum:XAUUSD", "XAUUSD")
        signals = strategy.compute_signals(bars, current_index, settings)
    """

    def __init__(self, strategy_id: str, symbol: str) -> None:
        super().__init__(strategy_id, symbol)

    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        if current_index < settings.lookback_period + 1:
            return self._hold_signal(bars, current_index, "momentum")

        # 仅使用已闭合数据：bars[:current_index+1]
        lookback = settings.lookback_period
        # 动量和波动率最多依赖 2 * lookback 期收益，无需复制全部历史。
        start = max(0, current_index - lookback * 2)
        closes = np.fromiter(
            (float(b.close) for b in bars[start : current_index + 1]),
            dtype=np.float64,
        )

        if len(closes) < lookback + 2:
            return self._hold_signal(bars, current_index, "momentum")

        # 计算历史收益率序列
        returns = np.diff(np.log(closes))

        # 过去 lookback 期的总收益率
        momentum_return = float(np.sum(returns[-lookback:]))

        # 历史收益率标准差（滚动窗口）
        hist_returns = returns[-lookback * 2 :] if len(returns) >= lookback * 2 else returns
        vol = float(np.std(hist_returns, ddof=1)) if len(hist_returns) > 1 else 1e-12

        if vol < 1e-12:
            return self._hold_signal(bars, current_index, "momentum")

        # 标准化动量信号
        normalized_momentum = momentum_return / vol
        entry_th = settings.entry_threshold
        exit_th = settings.exit_threshold

        current_close = closes[-1]
        timestamp = self._get_timestamp(bars, current_index)

        has_long = self.state.position > 0
        has_short = self.state.position < 0

        # ==== 信号逻辑 ====
        if normalized_momentum > entry_th and not has_long:
            # 买入信号 - 计算止损和仓位
            atr = self._calc_atr(bars, current_index, settings.atr_period)
            sl_price = current_close - atr * settings.stop_loss_atr if atr else current_close * 0.95
            tp_price = current_close + atr * settings.take_profit_atr if atr else current_close * 1.05
            return [
                Signal(
                    timestamp=timestamp,
                    symbol=self.symbol,
                    signal_type=SignalType.BUY,
                    price=current_close,
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    confidence=min(1.0, abs(normalized_momentum) / (entry_th * 3)),
                    metadata={
                        "momentum_return": momentum_return,
                        "normalized_momentum": normalized_momentum,
                        "atr": atr or 0.0,
                        "strategy": "momentum",
                    },
                )
            ]

        elif normalized_momentum < -entry_th and not has_short:
            # 卖出信号
            atr = self._calc_atr(bars, current_index, settings.atr_period)
            sl_price = current_close + atr * settings.stop_loss_atr if atr else current_close * 1.05
            tp_price = current_close - atr * settings.take_profit_atr if atr else current_close * 0.95
            return [
                Signal(
                    timestamp=timestamp,
                    symbol=self.symbol,
                    signal_type=SignalType.SELL,
                    price=current_close,
                    stop_loss=sl_price,
                    take_profit=tp_price,
                    confidence=min(1.0, abs(normalized_momentum) / (entry_th * 3)),
                    metadata={
                        "momentum_return": momentum_return,
                        "normalized_momentum": normalized_momentum,
                        "atr": atr or 0.0,
                        "strategy": "momentum",
                    },
                )
            ]

        elif abs(normalized_momentum) < exit_th and (has_long or has_short):
            # 平仓信号
            signal_type = SignalType.CLOSE_LONG if has_long else SignalType.CLOSE_SHORT
            return [
                Signal(
                    timestamp=timestamp,
                    symbol=self.symbol,
                    signal_type=signal_type,
                    metadata={"strategy": "momentum", "reason": "动量衰减"},
                )
            ]

        return self._hold_signal(bars, current_index, "momentum")
