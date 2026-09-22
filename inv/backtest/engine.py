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
    fee_contract_size: float | None = None


class BacktestEngine:
    """严格时间序列回测引擎。

    用法:
        engine = BacktestEngine(initial_equity=100000.0)
        result = engine.run(strategy, bars, settings, symbol_spec)
    """

    def __init__(
        self,
        initial_equity: float = 5_000.0,
        commission_pct: float = 0.001,  # 0.1% 手续费
        slippage_pct: float = 0.0005,  # 0.05% 滑点
        *,
        spread_pct: float = 0.0,
        funding_daily: float = 0.0,
        partial_fill_ratio: float = 1.0,
        trailing_atr: float = 0.0,
        risk_policy=None,
    ) -> None:
        self.initial_equity = float(initial_equity)
        self.commission_pct = float(commission_pct)
        self.slippage_pct = float(slippage_pct)
        from math import isfinite
        from inv.仓位风控 import RiskPolicy
        self.spread_pct = float(spread_pct)
        self.funding_daily = float(funding_daily)
        self.partial_fill_ratio = float(partial_fill_ratio)
        self.trailing_atr = float(trailing_atr)
        self.risk_policy = risk_policy or RiskPolicy()
        if not all(isfinite(v) for v in (self.initial_equity, self.commission_pct, self.slippage_pct,
                                         self.spread_pct, self.funding_daily, self.partial_fill_ratio, self.trailing_atr)):
            raise ValueError("回测配置必须为有限数值")
        if self.initial_equity <= 0 or min(self.commission_pct, self.slippage_pct, self.spread_pct, self.funding_daily, self.trailing_atr) < 0 or not 0 < self.partial_fill_ratio <= 1:
            raise ValueError("回测资金、成本或成交比例无效")

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
        from inv.交易会话 import TradingSession
        session = TradingSession(self, strategy, settings, symbol_spec)
        for bar in bars:
            session.on_bar(bar)
        return session.result()

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
        if state.position and (
            (signal.signal_type == SignalType.BUY and state.position.side == "long")
            or (signal.signal_type == SignalType.SELL and state.position.side == "short")
        ):
            # 单持仓引擎不能覆盖已有头寸，否则现金与交易记录失去对应关系。
            return state
        commission = volume * price * self.commission_pct * (state.fee_contract_size or contract_size)
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
        commission = pos.volume * exit_price * self.commission_pct * (state.fee_contract_size or contract_size)
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

        for protect_price, kind in [(pos.stop_loss, "sl"), (pos.take_profit, "tp")]:
            if protect_price is None:
                continue
            triggered = (
                (bar.low <= protect_price if pos.side == "long" else bar.high >= protect_price)
                if kind == "sl" else
                (bar.high >= protect_price if pos.side == "long" else bar.low <= protect_price)
            )
            if triggered:
                # 缺少逐笔路径时采用止损优先；止损跳空以更不利的开盘价成交。
                fill = protect_price
                if kind == "sl":
                    fill = min(bar.open, fill) if pos.side == "long" else max(bar.open, fill)
                self._close_position(state, fill, timestamp, trades, contract_size)
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
