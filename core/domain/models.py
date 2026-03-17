from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


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

