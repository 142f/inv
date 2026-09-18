"""领域层协议/接口定义

所有核心交互通过 Protocol 定义，便于测试和替换实现。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

from .models import (
    Bar, Tick, MarketSnapshot, OrderCommand,
    StrategyDecision, PerformanceMetrics, Signal, SignalType,
    StageInput, StageOutput, Inventory, SymbolSpec,
)
from .settings import StrategySettings, RiskSettings


@runtime_checkable
class BrokerProtocol(Protocol):
    """券商适配器协议，抽象 MT5 及其他交易终端。"""

    @property
    def lock(self) -> Any: ...
    @property
    def is_connected(self) -> bool: ...

    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...
    def ensure_symbol(self, symbol: str) -> bool: ...
    def account_info(self) -> Any: ...
    def terminal_info(self) -> Any: ...
    def orders_get(self) -> Any: ...
    def positions_get(self) -> Any: ...
    def symbol_info_tick(self, symbol: str) -> Any: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def order_send(self, request: dict) -> Any: ...
    def order_check(self, request: dict) -> Any: ...
    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any: ...
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any: ...
    def copy_rates_range(self, symbol: str, timeframe: Any, start: Any, end: Any) -> Any: ...
    def copy_ticks_range(self, symbol: str, start: Any, end: Any) -> Any: ...


@runtime_checkable
class DataProviderProtocol(Protocol):
    """仅读行情数据提供商协议。"""

    def bars(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "M1"
    ) -> Sequence[Bar]: ...

    def ticks(self, symbol: str, start: datetime, end: datetime) -> Sequence[Tick]: ...

    def specification(self, symbol: str) -> SymbolSpec: ...


@runtime_checkable
class StrategyProtocol(Protocol):
    """策略协议：每个策略必须实现以下接口。"""

    strategy_id: str
    symbol: str
    enabled: bool

    def on_data(self, snapshot: MarketSnapshot, inventory: Inventory, **kwargs: Any) -> StrategyDecision:
        """处理市场数据并返回交易决策。"""
        ...

    def on_tick(self, ctx: Any, *, action_collector: list | None = None) -> Any:
        """逐笔回调（兼容旧接口）。"""
        ...


@runtime_checkable
class PipelineStageProtocol(Protocol):
    """标准化流水线阶段协议。"""

    stage_name: str

    def process(self, stage_input: StageInput) -> StageOutput:
        """处理输入并返回输出。"""
        ...


@runtime_checkable
class SignalStrategyProtocol(Protocol):
    """产生标准化信号的策略协议。"""

    strategy_id: str
    symbol: str
    enabled: bool

    def compute_signals(
        self,
        bars: Sequence[Bar],
        current_index: int,
        settings: StrategySettings,
    ) -> Sequence[Signal]:
        """基于已闭合 K 线计算下一根 K 线的交易信号。

        Args:
            bars: 截止当前时间点的全部 K 线（不含未来数据）
            current_index: 当前处理的 K 线索引
            settings: 策略配置参数

        Returns:
            信号列表，将在下一根 K 线开盘时执行
        """
        ...


@runtime_checkable
class BacktestEngineProtocol(Protocol):
    """回测引擎协议，严格模拟时间推进。"""

    def run(
        self,
        strategy: SignalStrategyProtocol,
        bars: Sequence[Bar],
        settings: StrategySettings,
        symbol_spec: SymbolSpec,
    ) -> tuple[Sequence[float], Sequence[Any]]:
        """运行回测。

        Args:
            strategy: 待测策略
            bars: 历史 K 线序列
            settings: 策略参数
            symbol_spec: 品种规格

        Returns:
            (equity_curve, trades) 权益曲线与交易记录
        """
        ...


__all__ = [
    "BrokerProtocol", "DataProviderProtocol", "StrategyProtocol",
    "PipelineStageProtocol", "SignalStrategyProtocol",
    "BacktestEngineProtocol", "Signal",
]