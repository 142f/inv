"""统一领域模型 - 合并 core/domain/models.py 与 core/research/models.py

所有回测、实盘和研究的核心数据结构集中定义，消除重复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .enums import SignalType


# ===========================================================================
# 市场数据结构
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Bar:
    """标准 K 线数据，兼容 float 时间戳和 datetime。"""

    timestamp: float | datetime
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class Tick:
    """标准化逐笔行情。"""

    timestamp: datetime
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) * 0.5

    @property
    def spread_price(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass(slots=True)
class MarketSnapshot:
    """某个时间点的全市场快照，传递给策略执行回调。"""

    symbol: str
    tick: Any
    orders: Sequence[Any]
    positions: Sequence[Any]
    now: float
    atr: float | None = None
    bid: float = 0.0
    ask: float = 0.0


@dataclass(slots=True)
class ExposureSnapshot:
    """当前多空敞口快照。"""

    long_vol: float
    short_vol: float
    pending_buy_vol: float
    pending_sell_vol: float

    @property
    def net_vol(self) -> float:
        return self.long_vol + self.pending_buy_vol - self.short_vol - self.pending_sell_vol

    @property
    def gross_vol(self) -> float:
        return self.long_vol + self.short_vol + self.pending_buy_vol + self.pending_sell_vol


@dataclass(slots=True)
class Inventory:
    """库存信息，用于策略敞口管理。"""

    long_volume: float
    short_volume: float
    pending_buy_volume: float
    pending_sell_volume: float

    @property
    def net_volume(self) -> float:
        return self.long_volume + self.pending_buy_volume - self.short_volume - self.pending_sell_volume

    @property
    def gross_volume(self) -> float:
        return self.long_volume + self.short_volume + self.pending_buy_volume + self.pending_sell_volume


# ===========================================================================
# 品种规格
# ===========================================================================

@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """品种交易规则。领域层统一使用价格 tick 与手数步长，避免浮点比较漂移。"""

    symbol: str
    price_tick: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float = 1.0
    point: float = 0.00001
    digits: int = 5
    tick_size: float = 0.00001
    tick_value: float = 1.0
    swap_long: float | None = None
    swap_short: float | None = None
    swap_mode: str = "unknown"
    stops_level: int = 0
    freeze_level: int = 0

    def price_to_ticks(self, price: float) -> int:
        tick = max(float(self.price_tick), 1e-12)
        ratio = Decimal(str(price)) / Decimal(str(tick))
        return int(ratio.to_integral_value(rounding=ROUND_HALF_UP))

    def ticks_to_price(self, ticks: int) -> float:
        return float(int(ticks) * max(float(self.price_tick), 1e-12))

    def normalize_volume(self, volume: float) -> float:
        step = max(float(self.volume_step), 1e-12)
        ratio = Decimal(str(max(0.0, float(volume)))) / Decimal(str(step))
        steps = int(ratio.to_integral_value(rounding=ROUND_DOWN))
        normalized = float(Decimal(steps) * Decimal(str(step)))
        if normalized < float(self.volume_min):
            return 0.0
        return min(float(self.volume_max), normalized)

    def pnl(self, entry_price: float, exit_price: float, volume: float, side: str) -> float:
        """计算不含费用的盈亏。"""
        direction = 1.0 if side == "buy" else -1.0
        delta = (exit_price - entry_price) * direction
        if self.tick_value > 0:
            return delta / max(self.tick_size, 1e-12) * self.tick_value * volume
        return delta * self.contract_size * volume


# ===========================================================================
# 交易指令与结果
# ===========================================================================

@dataclass(frozen=True, slots=True)
class OrderCommand:
    """策略产生的幂等交易命令，基础设施层负责映射到券商请求。"""

    idempotency_key: str
    action: str  # CommandAction 的 value
    strategy_id: str
    symbol: str
    payload: Mapping[str, Any]
    priority: int = 100


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """纯策略层输出，不包含网络、文件或 MT5 副作用。"""

    strategy_id: str
    commands: tuple[OrderCommand, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """风控结论；halt 时执行端只允许撤单等降风险动作。"""

    allowed: bool
    halt: bool = False
    cancel_pending: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """执行结果的统一表示，兼容真实与纸面执行。"""

    accepted: bool
    simulated: bool
    retcode: int
    comment: str = ""
    order: int = 0
    duplicate: bool = False


# ===========================================================================
# 交易记录
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Trade:
    """已完成交易的记录。"""

    entry_time: float
    exit_time: float
    side: str
    volume: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    cost: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost


# ===========================================================================
# 绩效指标
# ===========================================================================

@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """统一绩效输出，所有金额均已扣除交易成本。"""

    net_profit: float
    volatility: float
    max_drawdown: float
    sharpe_ratio: float
    sortino_ratio: float
    win_rate: float
    payoff_ratio: float
    profit_factor: float
    risk_reward_ratio: float
    trade_count: int
    turnover: float
    cost_ratio: float
    cvar: float
    annualized_return: float = 0.0
    calmar_ratio: float = 0.0


# ===========================================================================
# 回测结果
# ===========================================================================

@dataclass(frozen=True, slots=True)
class BacktestResult:
    """单次回测的完整结果。"""

    equity_curve: tuple[float, ...]
    trades: tuple[Trade, ...]
    metrics: PerformanceMetrics
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


# ===========================================================================
# Pipeline 阶段间通信协议
# ===========================================================================

@dataclass(frozen=True, slots=True)
class StageInput:
    """pipeline 阶段的标准输入。"""

    symbol: str
    bars: tuple[Bar, ...]
    start_time: datetime
    end_time: datetime
    config: Mapping[str, Any] = field(default_factory=dict)
    previous_output: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StageOutput:
    """pipeline 阶段的标准输出。"""

    stage_name: str
    symbol: str
    timestamp: datetime
    data: Mapping[str, Any] = field(default_factory=dict)
    signals: tuple[SignalType, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


# ===========================================================================
# 策略信号（标准化信号格式）
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Signal:
    """标准化交易信号，用于策略与执行层之间的通信。"""

    timestamp: datetime
    symbol: str
    signal_type: SignalType
    price: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    volume: float = 0.0
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TargetBook:
    """策略的目标挂单簿。"""

    buy_targets: list[float] = field(default_factory=list)
    sell_targets: list[float] = field(default_factory=list)
    dynamic_buy_window: int = 0
    dynamic_sell_window: int = 0
    inventory_pressure: float = 0.0