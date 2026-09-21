"""严格时间序列回测引擎。

回测流程严格模拟：
  当前时间点已有数据 → 计算指标 → 生成信号 → 执行交易 → 推进到下一时间点

禁止：
- 使用未来数据
- 事后调整参数
- 整段读取后回溯决策
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from inv.domain.models import (
    Bar, Signal, SignalType, Trade, PerformanceMetrics,
    BacktestResult, SymbolSpec,
)
from inv.domain.settings import StrategySettings
from inv.strategy.base import BaseStrategy
from inv.backtest.metrics import calculate_metrics


@dataclass
class Position:
    """回测中的持仓状态。"""

    side: str  # "long" or "short"
    volume: float
    entry_price: float
    entry_time: float
    stop_loss: float | None = None
    take_profit: float | None = None
    entry_cost: float = 0.0

    @property
    def direction(self) -> float:
        return 1.0 if self.side == "long" else -1.0


@dataclass
class _ExecutionState:
    """回测执行中间状态（使用可变对象传递引用）。"""

    cash: float
    position: Position | None = None
    gross_commission: float = 0.0
    gross_slippage: float = 0.0
    max_position_volume: float = 0.0


class BacktestEngine:
    """严格时间序列回测引擎。

    用法:
        engine = BacktestEngine(initial_equity=100000.0)
        result = engine.run(strategy, bars, settings, symbol_spec)
    """

    def __init__(
        self,
        initial_equity: float = 100_000.0,
        commission_pct: float = 0.001,  # 0.1% 手续费
        slippage_pct: float = 0.0005,  # 0.05% 滑点
    ) -> None:
        self.initial_equity = float(initial_equity)
        self.commission_pct = float(commission_pct)
        self.slippage_pct = float(slippage_pct)

    def run(
        self,
        strategy: BaseStrategy,
        bars: Sequence[Bar],
        settings: StrategySettings,
        symbol_spec: SymbolSpec | None = None,
    ) -> BacktestResult:
        """运行一次完整回测。

        时间推进逻辑:
            for each bar[i] (已闭合):
                1. 用 bar[:i+1] 计算指标 → 生成信号
                2. 在 bar[i+1] 开盘执行信号
                3. 在 bar[i+1] 收盘时检查止损止盈
                4. 更新权益曲线
        """
        strategy.reset_state()

        state = _ExecutionState(cash=self.initial_equity)
        trades: list[Trade] = []
        equity_curve: list[float] = [self.initial_equity]
        contract_size = symbol_spec.contract_size if symbol_spec else 100.0

        bars_list = list(bars)
        bar_count = len(bars_list)

        for i in range(bar_count - 1):
            current_bar = bars_list[i]
            next_bar = bars_list[i + 1]

            # 第一步：使用已闭合的 K 线 bars[i] 计算信号
            signals = strategy.compute_signals(bars_list, i, settings)

            # 第二步：在下一根 K 线开盘时执行信号
            open_price = float(next_bar.open)
            timestamp = self._to_timestamp(next_bar.timestamp)

            for signal in signals:
                if signal.signal_type == SignalType.HOLD:
                    continue
                signal_price = signal.price if signal.price > 0 else open_price
                state = self._execute_signal(
                    state, signal, signal_price, timestamp, trades, contract_size
                )
                if state.position:
                    strategy.state.position = state.position.volume * state.position.direction
                    strategy.state.entry_price = state.position.entry_price
                else:
                    # 主动平仓后立即同步策略状态，避免后续 K 线仍被误判为持仓。
                    strategy.state.position = 0.0
                    strategy.state.entry_price = 0.0
                strategy.state.last_signal = signal.signal_type

            # 第三步：检查止损/止盈
            if state.position:
                state = self._check_protection(
                    state, next_bar, timestamp, trades, contract_size
                )
                if state.position is None:
                    strategy.state.position = 0.0
                    strategy.state.entry_price = 0.0

            # 第四步：按收盘价更新权益曲线
            close_price = float(next_bar.close)
            current_equity = self._mark_to_market(
                state, close_price, contract_size
            )
            equity_curve.append(current_equity)

            if state.position:
                state.max_position_volume = max(
                    state.max_position_volume, state.position.volume
                )

        # 最终平仓
        if state.position and bar_count > 1:
            last_bar = bars_list[-1]
            close_price = float(last_bar.close)
            state = self._force_close(
                state, close_price,
                self._to_timestamp(last_bar.timestamp),
                trades, contract_size,
            )
            equity_curve[-1] = state.cash

        # 计算绩效
        metrics = self._compute_metrics(equity_curve, trades, symbol_spec)

        return BacktestResult(
            equity_curve=tuple(equity_curve),
            trades=tuple(trades),
            metrics=metrics,
            diagnostics={
                "gross_commission": state.gross_commission,
                "gross_slippage": state.gross_slippage,
                "max_position_volume": state.max_position_volume,
                "total_costs": state.gross_commission + state.gross_slippage,
                "final_equity": equity_curve[-1] if equity_curve else self.initial_equity,
            },
        )

    def _execute_signal(
        self,
        state: _ExecutionState,
        signal: Signal,
        price: float,
        timestamp: float,
        trades: list[Trade],
        contract_size: float,
    ) -> _ExecutionState:
        """执行信号，返回更新后的状态。"""
        volume = signal.volume if signal.volume > 0 else 1.0
        commission = volume * price * self.commission_pct * contract_size
        slippage = volume * price * self.slippage_pct * contract_size

        if signal.signal_type == SignalType.BUY:
            # 先平空
            if state.position and state.position.side == "short":
                self._close_position(state, price, timestamp, trades, contract_size)
            # 开多
            cost = volume * price * contract_size + commission + slippage
            state.cash -= cost
            state.gross_commission += commission
            state.gross_slippage += slippage
            state.position = Position(
                side="long", volume=volume, entry_price=price,
                entry_time=timestamp, stop_loss=signal.stop_loss,
                take_profit=signal.take_profit, entry_cost=commission + slippage,
            )

        elif signal.signal_type == SignalType.SELL:
            if state.position and state.position.side == "long":
                self._close_position(state, price, timestamp, trades, contract_size)
            # 开空
            revenue = volume * price * contract_size - commission - slippage
            state.cash += revenue
            state.gross_commission += commission
            state.gross_slippage += slippage
            state.position = Position(
                side="short", volume=volume, entry_price=price,
                entry_time=timestamp, stop_loss=signal.stop_loss,
                take_profit=signal.take_profit, entry_cost=commission + slippage,
            )

        elif signal.signal_type == SignalType.CLOSE_LONG and state.position and state.position.side == "long":
            self._close_position(state, price, timestamp, trades, contract_size)
            state.position = None

        elif signal.signal_type == SignalType.CLOSE_SHORT and state.position and state.position.side == "short":
            self._close_position(state, price, timestamp, trades, contract_size)
            state.position = None

        return state

    def _close_position(
        self,
        state: _ExecutionState,
        exit_price: float,
        exit_time: float,
        trades: list[Trade],
        contract_size: float,
    ) -> None:
        """平仓并记录交易。"""
        if state.position is None:
            return
        pos = state.position
        gross_pnl = (exit_price - pos.entry_price) * pos.direction * pos.volume * contract_size
        commission = pos.volume * exit_price * self.commission_pct * contract_size
        slippage = pos.volume * exit_price * self.slippage_pct * contract_size
        exit_cost = commission + slippage

        state.gross_commission += commission
        state.gross_slippage += slippage

        # 现金调整：多头卖出收回资金，空头买回支付资金
        if pos.side == "long":
            state.cash += pos.volume * exit_price * contract_size - exit_cost
        else:
            state.cash -= pos.volume * exit_price * contract_size + exit_cost

        trades.append(Trade(
            entry_time=pos.entry_time, exit_time=exit_time,
            side=pos.side, volume=pos.volume,
            entry_price=pos.entry_price, exit_price=exit_price,
            gross_pnl=gross_pnl, cost=pos.entry_cost + exit_cost,
        ))

    def _check_protection(
        self,
        state: _ExecutionState,
        bar: Bar,
        timestamp: float,
        trades: list[Trade],
        contract_size: float,
    ) -> _ExecutionState:
        """检查止损/止盈是否触发。"""
        pos = state.position
        if pos is None:
            return state

        for protect_price, _ in [(pos.take_profit, "tp"), (pos.stop_loss, "sl")]:
            if protect_price is None:
                continue
            if bar.low <= protect_price <= bar.high:
                self._close_position(state, protect_price, timestamp, trades, contract_size)
                state.position = None
                break
        return state

    def _force_close(
        self,
        state: _ExecutionState,
        price: float,
        timestamp: float,
        trades: list[Trade],
        contract_size: float,
    ) -> _ExecutionState:
        """回测结束时强制平仓。"""
        self._close_position(state, price, timestamp, trades, contract_size)
        state.position = None
        return state

    @staticmethod
    def _mark_to_market(
        state: _ExecutionState, current_price: float, contract_size: float
    ) -> float:
        """计算当前权益。"""
        if state.position is None:
            return state.cash
        market_value = current_price * state.position.volume * contract_size
        return state.cash + state.position.direction * market_value

    @staticmethod
    def _to_timestamp(value: float | datetime) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        return value.timestamp()

    @staticmethod
    def _compute_metrics(
        equity_curve: list[float],
        trades: list[Trade],
        symbol_spec: SymbolSpec | None,
    ) -> PerformanceMetrics:
        if not trades:
            return PerformanceMetrics(
                net_profit=0.0, volatility=0.0, max_drawdown=0.0,
                sharpe_ratio=0.0, sortino_ratio=0.0, win_rate=0.0,
                payoff_ratio=0.0, profit_factor=0.0, risk_reward_ratio=0.0,
                trade_count=0, turnover=0.0, cost_ratio=0.0, cvar=0.0,
                annualized_return=0.0, calmar_ratio=0.0,
            )
        periods = 365 if symbol_spec and "BTC" in symbol_spec.symbol.upper() else 252
        return calculate_metrics(equity_curve, trades, periods_per_year=periods)


__all__ = ["BacktestEngine"]
