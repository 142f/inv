"""Normalize strategy config values for runtime updates and creation."""

from __future__ import annotations

from typing import Any, Dict


_BOOL_KEYS = {
    "enabled",
    "use_atr",
    "adaptive_enabled",
    "hedge_enabled",
    "auto_trim",
}

_INT_KEYS = {
    "magic",
    "window",
    "atr_period",
    "buy_window",
    "sell_window",
    "recenter_steps",
    "max_long_pos",
    "max_short_pos",
    "max_new_orders_per_update",
    "hedge_tranches",
    "hedge_entry_steps",
    "hedge_exit_steps",
    "hedge_vol_lookback",
    "hedge_vol_window",
    "hedge_vol_base",
    "be_trigger_steps",
    "be_buffer_points",
    "adaptive_lookback",
}

_FLOAT_KEYS = {
    "step",
    "tp_dist",
    "sl_dist",
    "lot",
    "min_p",
    "max_p",
    "atr_factor",
    "atr_update_seconds",
    "atr_smooth",
    "atr_change_threshold",
    "min_step_mult",
    "max_step_mult",
    "adaptive_quantile_low",
    "adaptive_quantile_high",
    "adaptive_step_mult_low",
    "adaptive_step_mult_high",
    "adaptive_lot_min_mult",
    "adaptive_lot_max_mult",
    "adaptive_range_buffer_atr",
    "recenter_cooldown",
    "max_long_vol",
    "max_short_vol",
    "max_net_vol",
    "max_spread_points",
    "extreme_cooldown",
    "hedge_fraction",
    "hedge_cooldown",
    "max_gross_vol",
    "hedge_vol_quantile",
    "hedge_vol_mult",
}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _coerce_int(value: Any) -> Any:
    if value is None:
        return None
    try:
        return int(float(value))
    except Exception:
        return value


def _coerce_float(value: Any) -> Any:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return value


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        return {}

    normalized: Dict[str, Any] = {}
    for key, value in cfg.items():
        if key in _BOOL_KEYS:
            normalized[key] = _coerce_bool(value)
        elif key in _INT_KEYS:
            normalized[key] = _coerce_int(value)
        elif key in _FLOAT_KEYS:
            normalized[key] = _coerce_float(value)
        else:
            normalized[key] = value
    return normalized
