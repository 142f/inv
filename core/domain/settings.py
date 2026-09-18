"""类型化策略与风控配置。

本模块只处理调用方提供的映射，不读取任何配置文件，从而保持敏感配置的访问边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import ExecutionMode


def _optional_float(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return int(float(value))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


@dataclass(frozen=True, slots=True)
class RiskSettings:
    """策略级风险预算；缺少预算时只能运行在纸面模式。"""

    execution_mode: ExecutionMode = ExecutionMode.PAPER
    live_enabled: bool = False
    max_net_volume: float | None = None
    max_gross_volume: float | None = None
    max_notional: float | None = None
    max_open_orders: int | None = None
    max_actions_per_cycle: int = 10
    min_margin_level: float | None = None
    max_drawdown_ratio: float | None = None
    max_daily_loss: float | None = None
    max_spread_points: float | None = None
    max_tick_age_seconds: float | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RiskSettings":
        values = dict(raw or {})
        nested = values.get("risk")
        if isinstance(nested, Mapping):
            values = {**values, **nested}
        mode_raw = str(values.get("execution_mode", "paper") or "paper").strip().lower()
        mode = ExecutionMode.LIVE if mode_raw == ExecutionMode.LIVE.value else ExecutionMode.PAPER
        return cls(
            execution_mode=mode,
            live_enabled=_as_bool(values.get("live_enabled", False)),
            max_net_volume=_optional_float(values.get("risk_max_net_vol", values.get("max_net_vol"))),
            max_gross_volume=_optional_float(values.get("risk_max_gross_vol", values.get("max_gross_vol"))),
            max_notional=_optional_float(values.get("max_notional")),
            max_open_orders=_optional_int(values.get("max_open_orders")),
            max_actions_per_cycle=max(1, int(values.get("max_actions_per_cycle", 10) or 10)),
            min_margin_level=_optional_float(values.get("min_margin_level")),
            max_drawdown_ratio=_optional_float(values.get("max_drawdown_ratio")),
            max_daily_loss=_optional_float(values.get("max_daily_loss")),
            max_spread_points=_optional_float(values.get("risk_max_spread_points", values.get("max_spread_points"))),
            max_tick_age_seconds=_optional_float(values.get("max_tick_age_seconds")),
        )

    @property
    def can_trade_live(self) -> bool:
        """实盘必须显式开启且包含最小的暴露与回撤预算。"""
        return (
            self.execution_mode is ExecutionMode.LIVE
            and self.live_enabled
            and self.max_net_volume is not None
            and self.max_drawdown_ratio is not None
            and self.max_tick_age_seconds is not None
        )


@dataclass(frozen=True, slots=True)
class StrategySettings:
    """兼容旧网格字段的不可变配置快照。"""

    symbol: str
    magic: int
    step: float
    tp_dist: float
    lot: float
    enabled: bool = True
    legacy: Mapping[str, Any] = field(default_factory=dict)
    risk: RiskSettings = field(default_factory=RiskSettings)

    @property
    def strategy_id(self) -> str:
        return f"{self.magic}:{self.symbol}"

    @classmethod
    def from_legacy(cls, raw: Mapping[str, Any]) -> "StrategySettings":
        values = dict(raw)
        return cls(
            symbol=str(values["symbol"]),
            magic=int(float(values["magic"])),
            step=float(values["step"]),
            tp_dist=float(values["tp_dist"]),
            lot=float(values["lot"]),
            enabled=_as_bool(values.get("enabled", True)),
            legacy=values,
            risk=RiskSettings.from_mapping(values),
        )
