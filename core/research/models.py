"""研究模块的数据结构，不读取也不写入交易数据文件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Tuple

from core.domain import PerformanceMetrics


@dataclass(frozen=True, slots=True)
class Bar:
    # 兼容旧回测器使用的 Unix 秒与新历史适配器使用的 UTC 时间。
    timestamp: float | datetime
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class CostModel:
    """所有成本均以价格单位表示，便于在不同合约模型中替换。"""

    commission_per_volume: float = 0.0
    slippage_points: float = 0.0
    point: float = 0.00001
    minimum_fill_volume: float = 0.0
    max_volume_share: float = 1.0

    def transaction_cost(self, volume: float) -> float:
        return abs(float(volume)) * (self.commission_per_volume + self.slippage_points * self.point)


@dataclass(frozen=True, slots=True)
class Trade:
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


@dataclass(frozen=True, slots=True)
class BacktestResult:
    equity_curve: Tuple[float, ...]
    trades: Tuple[Trade, ...]
    metrics: PerformanceMetrics
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
