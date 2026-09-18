from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
from typing import Any, Mapping, List, Optional, Tuple


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    tick: Any
    orders: List[Any]
    positions: List[Any]
    now: float
    atr: Optional[float] = None


@dataclass(slots=True)
class ExposureSnapshot:
    long_vol: float
    short_vol: float
    pending_buy_vol: float
    pending_sell_vol: float
    net_vol: float
    predicted_net_vol: float


@dataclass(slots=True)
class TargetBook:
    buy_targets: List[float] = field(default_factory=list)
    sell_targets: List[float] = field(default_factory=list)
    dynamic_buy_window: int = 0
    dynamic_sell_window: int = 0
    inventory_pressure: float = 0.0


class ExecutionMode(str, Enum):
    """执行模式；除非显式启用，否则所有交易命令只能进入纸面账本。"""

    PAPER = "paper"
    LIVE = "live"


class CommandAction(str, Enum):
    """与券商无关的交易意图。"""

    PLACE_LIMIT = "place_limit"
    CANCEL_ORDER = "cancel_order"
    MODIFY_PROTECTION = "modify_protection"
    OPEN_HEDGE = "open_hedge"
    CLOSE_POSITION = "close_position"


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """品种交易规则。领域层统一使用价格 tick 与手数步长，避免浮点比较漂移。"""

    symbol: str
    price_tick: float
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float = 1.0

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


@dataclass(frozen=True, slots=True)
class OrderCommand:
    """策略产生的幂等交易命令，基础设施层负责映射到券商请求。"""

    idempotency_key: str
    action: CommandAction
    strategy_id: str
    symbol: str
    payload: Mapping[str, Any]
    priority: int = 100


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """纯策略层输出，不包含网络、文件或 MT5 副作用。"""

    strategy_id: str
    commands: Tuple[OrderCommand, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """风控结论；halt 时执行端只允许撤单等降风险动作。"""

    allowed: bool
    halt: bool = False
    cancel_pending: bool = False
    reasons: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """执行结果的统一表示，兼容真实与纸面执行。"""

    accepted: bool
    simulated: bool
    retcode: int
    comment: str = ""
    order: int = 0
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """研究层的统一绩效输出，所有金额均应已扣除交易成本。"""

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
