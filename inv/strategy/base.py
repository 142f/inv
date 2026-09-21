"""策略基类与协议实现。

所有策略继承 BaseStrategy，共享信号生成生命周期和仓位管理逻辑。
核心原则：
1. 仅使用已闭合的 K 线数据（无未来数据）
2. 信号在当期 K 线收盘后产生，在下一期开盘执行
3. 策略状态仅依赖历史数据
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from inv.domain.models import (
    Bar, Signal, SignalType, MarketSnapshot,
    Inventory, StrategyDecision, SymbolSpec,
    Tick, Trade,
)
from inv.domain.settings import StrategySettings


@dataclass
class StrategyState:
    """策略运行时状态，在回测或实盘中被持续更新。"""

    position: float = 0.0  # 当前持仓（正数=多，负数=空）
    entry_price: float = 0.0  # 开仓均价
    last_signal: SignalType = SignalType.HOLD
    consecutive_losses: int = 0
    total_trades: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        self.position = 0.0
        self.entry_price = 0.0
        self.last_signal = SignalType.HOLD
        self.consecutive_losses = 0
        self.total_trades = 0
        self.diagnostics.clear()


class BaseStrategy(ABC):
    """策略基类，所有策略必须实现 compute_signals 方法。

    回测流程:
    for i in range(len(bars)):
        # bar[i] 是已闭合的 K 线
        signals = strategy.compute_signals(bars[:i+1], i, settings)
        # signals 在下一根 K 线 bar[i+1] 开盘时执行
    """

    def __init__(self, strategy_id: str, symbol: str) -> None:
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.state = StrategyState()
        self._cache: dict[str, Any] = {}

    @abstractmethod
    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        """基于已闭合 K 线计算下一根 K 线的交易信号。"""
        ...

    def update_state(self, trade: Trade | None = None) -> None:
        """更新策略状态（可选覆写）。"""
        if trade is not None:
            self.state.total_trades += 1
            if trade.net_pnl < 0:
                self.state.consecutive_losses += 1
            else:
                self.state.consecutive_losses = 0

    def reset_state(self) -> None:
        """重置策略状态。"""
        self.state.reset()
        self._cache.clear()

    @staticmethod
    def _get_timestamp(bars: Sequence[Bar], current_index: int) -> datetime:
        """返回当前 K 线的 UTC 时间，并兼容空序列。"""
        if not bars:
            return datetime.now(timezone.utc)
        ts = bars[min(max(0, current_index), len(bars) - 1)].timestamp
        if isinstance(ts, datetime):
            return ts
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)

    def _hold_signal(
        self,
        bars: Sequence[Bar],
        current_index: int,
        strategy_name: str,
    ) -> list[Signal]:
        """生成格式一致的空仓信号。"""
        return [
            Signal(
                timestamp=self._get_timestamp(bars, current_index),
                symbol=self.symbol,
                signal_type=SignalType.HOLD,
                metadata={"strategy": strategy_name},
            )
        ]

    @staticmethod
    def _calc_atr(
        bars: Sequence[Bar], current_index: int, period: int
    ) -> float | None:
        """仅扫描计算 ATR 所需的有界窗口。"""
        if period <= 0 or current_index < period + 1:
            return None
        start = current_index - period
        tail = bars[start : current_index + 1]
        if len(tail) < period + 1:
            return None
        true_range_sum = 0.0
        for prev, curr in zip(tail, tail[1:]):
            true_range_sum += max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
        return true_range_sum / period

    def calculate_position_size(
        self,
        price: float,
        stop_loss: float,
        equity: float,
        risk_per_trade: float,
        symbol_spec: SymbolSpec,
    ) -> float:
        """基于风险计算仓位大小。"""
        risk_amount = equity * risk_per_trade
        price_risk = abs(price - stop_loss)
        if price_risk < 1e-12:
            return symbol_spec.volume_min
        raw_volume = risk_amount / (price_risk * symbol_spec.contract_size)
        return symbol_spec.normalize_volume(raw_volume)

    def get_signal_confidence(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> float:
        """计算信号置信度（0~1），可选覆写。"""
        return 1.0


class CompositeStrategy(BaseStrategy):
    """组合策略：运行多个子策略，通过投票或加权产生最终信号。"""

    def __init__(
        self,
        strategy_id: str,
        symbol: str,
        sub_strategies: list[BaseStrategy],
        weights: list[float] | None = None,
    ) -> None:
        super().__init__(strategy_id, symbol)
        self.sub_strategies = sub_strategies
        self.weights = weights or [1.0] * len(sub_strategies)
        total = sum(self.weights)
        self.weights = [w / total for w in self.weights]

    def _resolve_timestamp(self, bars: Sequence[Bar]) -> datetime:
        """从 K 线序列中解析出当前时间戳。"""
        return self._get_timestamp(bars, len(bars) - 1)

    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        """通过加权投票产生组合信号。"""
        buy_score = 0.0
        sell_score = 0.0
        all_signals: list[Signal] = []

        for strategy, weight in zip(self.sub_strategies, self.weights):
            signals = strategy.compute_signals(bars, current_index, settings)
            all_signals.extend(signals)
            for sig in signals:
                if sig.signal_type in (SignalType.BUY, SignalType.CLOSE_SHORT):
                    buy_score += weight * sig.confidence
                elif sig.signal_type in (SignalType.SELL, SignalType.CLOSE_LONG):
                    sell_score += weight * sig.confidence

        timestamp = self._resolve_timestamp(bars)
        threshold = 0.5
        if buy_score > sell_score and buy_score > threshold:
            return [Signal(
                timestamp=timestamp, symbol=self.symbol,
                signal_type=SignalType.BUY, confidence=buy_score,
            )]
        elif sell_score > buy_score and sell_score > threshold:
            return [Signal(
                timestamp=timestamp, symbol=self.symbol,
                signal_type=SignalType.SELL, confidence=sell_score,
            )]
        return [Signal(
            timestamp=timestamp, symbol=self.symbol,
            signal_type=SignalType.HOLD,
        )]


__all__ = ["BaseStrategy", "StrategyState", "CompositeStrategy"]
