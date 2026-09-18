"""类型化策略与风控配置。

本模块只处理调用方提供的映射，不读取任何配置文件，从而保持敏感配置的访问边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .enums import ExecutionMode


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
            max_spread_points=_optional_float(
                values.get("risk_max_spread_points", values.get("max_spread_points"))
            ),
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
    """策略配置快照，通用版本。"""

    symbol: str
    magic: int
    enabled: bool = True
    strategy_type: str = "grid"  # grid, momentum, mean_reversion, breakout
    legacy: Mapping[str, Any] = field(default_factory=dict)
    risk: RiskSettings = field(default_factory=RiskSettings)

    # --- 通用参数 ---
    step: float = 0.0
    lot: float = 0.01

    # --- 网格参数 ---
    tp_dist: float = 0.0
    sl_dist: float = 0.0
    window: int = 6
    min_price: float = 0.0
    max_price: float = 999999.0
    mode: str = "neutral"

    # --- ATR 参数 ---
    use_atr: bool = False
    atr_period: int = 14
    atr_factor: float = 1.0
    atr_mode: str = "wilder"
    atr_timeframe: str = "M15"

    # --- 动量/均值回归参数 ---
    lookback_period: int = 20
    entry_threshold: float = 1.5  # 标准差倍数
    exit_threshold: float = 0.5
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 3.0
    max_positions: int = 1

    # --- 突破策略参数 ---
    breakout_lookback: int = 20
    breakout_atr_mult: float = 2.0
    breakout_confirmation_bars: int = 1

    # --- 仓位管理 ---
    risk_per_trade: float = 0.01  # 每笔交易风险比例
    max_net_vol: float | None = None
    max_short_vol: float | None = None
    max_long_vol: float | None = None
    max_gross_vol: float | None = None

    @property
    def strategy_id(self) -> str:
        return f"{self.magic}:{self.symbol}"

    @classmethod
    def from_legacy(cls, raw: Mapping[str, Any]) -> "StrategySettings":
        values = dict(raw)
        return cls(
            symbol=str(values["symbol"]),
            magic=int(float(values["magic"])),
            enabled=_as_bool(values.get("enabled", True)),
            strategy_type=str(values.get("strategy_type", "grid")),
            step=float(values.get("step", 0.0)),
            lot=float(values.get("lot", 0.01)),
            tp_dist=float(values.get("tp_dist", 0.0)),
            sl_dist=float(values.get("sl_dist", 0.0)),
            window=int(values.get("window", 6)),
            min_price=float(values.get("min_p", 0.0)),
            max_price=float(values.get("max_p", 999999.0)),
            mode=str(values.get("mode", "neutral")),
            use_atr=_as_bool(values.get("use_atr", False)),
            atr_period=int(values.get("atr_period", 14)),
            atr_factor=float(values.get("atr_factor", 1.0)),
            atr_mode=str(values.get("atr_mode", "wilder")),
            atr_timeframe=str(values.get("atr_timeframe", "M15")),
            lookback_period=int(values.get("lookback_period", 20)),
            entry_threshold=float(values.get("entry_threshold", 1.5)),
            exit_threshold=float(values.get("exit_threshold", 0.5)),
            stop_loss_atr=float(values.get("stop_loss_atr", 2.0)),
            take_profit_atr=float(values.get("take_profit_atr", 3.0)),
            max_positions=int(values.get("max_positions", 1)),
            breakout_lookback=int(values.get("breakout_lookback", 20)),
            breakout_atr_mult=float(values.get("breakout_atr_mult", 2.0)),
            breakout_confirmation_bars=int(values.get("breakout_confirmation_bars", 1)),
            risk_per_trade=float(values.get("risk_per_trade", 0.01)),
            max_net_vol=_optional_float(values.get("max_net_vol")),
            max_short_vol=_optional_float(values.get("max_short_vol")),
            max_long_vol=_optional_float(values.get("max_long_vol")),
            max_gross_vol=_optional_float(values.get("max_gross_vol")),
            legacy=values,
            risk=RiskSettings.from_mapping(values),
        )


__all__ = ["RiskSettings", "StrategySettings"]