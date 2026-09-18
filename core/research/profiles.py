"""标准化回测的公开配置与成本模型。

本模块不读取策略配置、账户信息或任何历史文件；所有假设均由调用方显式传入，
以便每一次实验都可以复现和审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


UTC = timezone.utc


class CostScenario(str, Enum):
    """成本压力情景。"""

    BASELINE = "baseline"
    STRESS = "stress"


class CommissionUnit(str, Enum):
    """佣金报价单位。"""

    USD_PER_LOT = "usd_per_lot"
    BASIS_POINTS = "basis_points"


@dataclass(frozen=True, slots=True)
class CostProfile:
    """单个品种的显式成本假设。

    ``slippage_price`` 用于以价格单位报价的品种（例如 XAUUSD）；
    ``slippage_bps`` 用于按成交名义金额报价的品种（例如 BTCUSD）。
    同一笔开仓或平仓只会采用其中一种滑点规则。
    """

    name: str
    commission_unit: CommissionUnit
    commission_value: float
    normal_slippage_price: float = 0.0
    stress_slippage_price: float = 0.0
    normal_slippage_bps: float = 0.0
    stress_slippage_bps: float = 0.0
    fallback_daily_funding_bps: float | None = None
    stress_daily_funding_bps: float | None = None
    source: str = "公开标准化假设"

    def __post_init__(self) -> None:
        if self.commission_value < 0:
            raise ValueError("佣金不能为负数")
        if min(
            self.normal_slippage_price,
            self.stress_slippage_price,
            self.normal_slippage_bps,
            self.stress_slippage_bps,
        ) < 0:
            raise ValueError("滑点不能为负数")
        if self.commission_unit is CommissionUnit.USD_PER_LOT and self.commission_value == 0:
            raise ValueError("按手数计费时必须提供佣金")

    def slippage(self, price: float, scenario: CostScenario) -> float:
        """返回单边滑点的绝对价格，始终为正数。"""

        if scenario is CostScenario.STRESS:
            return max(
                self.stress_slippage_price,
                price * self.stress_slippage_bps / 10_000.0,
            )
        return max(
            self.normal_slippage_price,
            price * self.normal_slippage_bps / 10_000.0,
        )

    def commission(self, price: float, volume: float, contract_size: float) -> float:
        """计算单边佣金（USD）。"""

        if self.commission_unit is CommissionUnit.USD_PER_LOT:
            return self.commission_value * volume
        return price * volume * contract_size * self.commission_value / 10_000.0

    def fallback_funding_bps(self, scenario: CostScenario) -> float | None:
        if scenario is CostScenario.STRESS:
            return self.stress_daily_funding_bps
        return self.fallback_daily_funding_bps


@dataclass(frozen=True, slots=True)
class BacktestProfile:
    """XAUUSD/BTCUSD 横向比较的公开且无副作用的实验参数。"""

    start: datetime = datetime(2024, 1, 1, tzinfo=UTC)
    end: datetime = datetime(2026, 8, 19, 23, 59, 59, tzinfo=UTC)
    initial_equity: float = 100_000.0
    target_annual_volatility: float = 0.15
    ewma_span_days: int = 20
    grid_levels_per_side: int = 6
    atr_period: int = 14
    grid_step_atr: float = 1.0
    take_profit_atr: float = 1.0
    stop_loss_atr: float = 3.0
    max_gross_notional_equity_ratio: float = 1.0
    max_net_notional_gross_ratio: float = 0.5
    max_open_orders: int = 24
    partial_fill_ratio: float = 1.0
    xau_symbol: str = "XAUUSD"
    btc_symbol: str = "BTCUSD"
    tick_review_windows: int = 3
    tick_review_days: int = 7
    required_m1_coverage: float = 0.995

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("回测起止时间必须带 UTC 时区")
        if self.start >= self.end:
            raise ValueError("回测起始时间必须早于结束时间")
        if self.initial_equity <= 0 or not 0 < self.target_annual_volatility <= 1:
            raise ValueError("初始权益或目标波动率无效")
        if self.ewma_span_days < 2 or self.atr_period < 2 or self.grid_levels_per_side < 1:
            raise ValueError("指标窗口或网格层数无效")
        if not 0 < self.partial_fill_ratio <= 1:
            raise ValueError("部分成交比例必须在 (0, 1] 区间")
        if not 0 < self.required_m1_coverage <= 1:
            raise ValueError("M1 覆盖率必须在 (0, 1] 区间")

    @property
    def target_orders(self) -> int:
        return self.grid_levels_per_side * 2

    def resolve_symbol(self, canonical: str, suffix: str = "") -> str:
        """将标准品种名映射为经纪商后缀品种名。"""

        return f"{canonical}{suffix}"


XAUUSD_STANDARD_COST = CostProfile(
    name="XAUUSD 标准成本",
    commission_unit=CommissionUnit.USD_PER_LOT,
    commission_value=7.0,
    normal_slippage_price=0.05,
    stress_slippage_price=0.20,
    source="用户提供：7 USD/lot/边，滑点 0.05/0.20 USD/oz",
)

BTCUSD_STANDARD_COST = CostProfile(
    name="BTCUSD 标准成本",
    commission_unit=CommissionUnit.BASIS_POINTS,
    commission_value=5.0,
    normal_slippage_bps=5.0,
    stress_slippage_bps=20.0,
    fallback_daily_funding_bps=5.0,
    stress_daily_funding_bps=15.0,
    source="公开标准化假设：5 bps/边佣金、5/20 bps 滑点、5/15 bps/日资金费率",
)


def xau_swap_sensitivity() -> tuple[float, float, float]:
    """在经纪商未提供 swap 时使用的 XAUUSD 每手每日敏感性情景。"""

    return (0.0, -20.0, -40.0)

