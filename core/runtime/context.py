"""Runtime strategy context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StrategyContext:
    tick: Any
    orders: list
    positions: list
    account: Any
    atr: float | None = None
    now: float | None = None
