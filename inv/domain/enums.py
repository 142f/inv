"""领域层枚举类型定义"""

from __future__ import annotations

from enum import Enum


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
    PLACE_MARKET = "place_market"


class CostScenario(str, Enum):
    """成本压力情景。"""

    BASELINE = "baseline"
    STRESS = "stress"


class CommissionUnit(str, Enum):
    """佣金报价单位。"""

    USD_PER_LOT = "usd_per_lot"
    BASIS_POINTS = "basis_points"


class SignalType(str, Enum):
    """策略信号类型，用于标准化阶段间通信。"""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    FLAT = "flat"