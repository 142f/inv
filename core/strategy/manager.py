"""Strategy lifecycle: config normalization, hot-update, and manager."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Set

from core.logger import Logger
from core.runtime.datafeed import DataFeed
from core.strategy.grid_strategy.strategy import GridStrategy

# ── Config normalization (merged from config.py) ──────────────────────────────

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


def _build_coercers_by_key() -> Dict[str, Callable[[Any], Any]]:
    coercers: Dict[str, Callable[[Any], Any]] = {}
    coercers.update({key: _coerce_bool for key in _BOOL_KEYS})
    coercers.update({key: _coerce_int for key in _INT_KEYS})
    coercers.update({key: _coerce_float for key in _FLOAT_KEYS})
    return coercers


_COERCERS_BY_KEY = _build_coercers_by_key()


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        return {}

    normalized: Dict[str, Any] = {}
    for key, value in cfg.items():
        coercer = _COERCERS_BY_KEY.get(key)
        normalized[key] = coercer(value) if coercer is not None else value
    return normalized


# ── Strategy updater (merged from updater.py) ─────────────────────────────────

class StrategyUpdater:
    _ATR_KEYS = (
        "use_atr",
        "atr_period",
        "atr_factor",
        "atr_mode",
        "atr_timeframe",
        "atr_update_seconds",
        "atr_smooth",
        "atr_change_threshold",
        "min_step_mult",
        "max_step_mult",
    )

    _ADAPTIVE_KEYS = (
        "adaptive_enabled",
        "adaptive_timeframe",
        "adaptive_lookback",
        "adaptive_quantile_low",
        "adaptive_quantile_high",
        "adaptive_step_mult_low",
        "adaptive_step_mult_high",
        "adaptive_lot_min_mult",
        "adaptive_lot_max_mult",
        "adaptive_range_buffer_atr",
    )

    _GENERAL_KEYS = (
        "mode",
        "out_of_range_action",
        "recenter_steps",
        "recenter_cooldown",
        "max_long_pos",
        "max_short_pos",
        "max_long_vol",
        "max_short_vol",
        "max_net_vol",
        "max_spread_points",
        "extreme_cooldown",
        "max_new_orders_per_update",
        "auto_trim",
    )

    _HEDGE_KEYS = (
        "hedge_enabled",
        "hedge_fraction",
        "hedge_tranches",
        "hedge_entry_steps",
        "hedge_exit_steps",
        "hedge_cooldown",
        "max_gross_vol",
        "hedge_vol_lookback",
        "hedge_vol_window",
        "hedge_vol_quantile",
        "hedge_vol_base",
        "hedge_vol_mult",
        "be_trigger_steps",
        "be_buffer_points",
    )

    _ORDER_RELATED_KEYS = (
        "step",
        "tp_dist",
        "sl_dist",
        "lot",
        "window",
        "buy_window",
        "sell_window",
        "min_price",
        "max_price",
        "mode",
        "auto_trim",
    )

    def __init__(self, broker):
        self.broker = broker

    def apply(self, strategy: GridStrategy, cfg: dict):
        cfg = normalize_config(cfg)
        current_state = strategy.get_state()
        current_state.pop("enabled", None)
        cleared_orders = False

        before = self._snapshot_order_related_state(strategy)
        atr_before = self._snapshot(strategy, self._ATR_KEYS)
        adaptive_before = self._snapshot(strategy, self._ADAPTIVE_KEYS)

        new_symbol = cfg.get("symbol", strategy.symbol)
        if strategy.symbol != new_symbol:
            strategy.clear_old_orders()
            cleared_orders = True
            self.broker.ensure_symbol(new_symbol)
            strategy.set_symbol(new_symbol, reset_runtime_state=True)
            current_state = {}
            Logger.log(
                "SYSTEM",
                "UPDATE",
                f"Strategy {strategy.magic} symbol: {before['symbol']} -> {strategy.symbol}",
            )

        self._apply_base_updates(strategy, cfg)
        self._apply_window_and_range_updates(strategy, cfg)
        self._apply_updates(strategy, cfg, self._ATR_KEYS)
        self._apply_updates(strategy, cfg, self._GENERAL_KEYS, coerce_value=self._coerce_general_value)
        self._apply_updates(strategy, cfg, self._HEDGE_KEYS)

        atr_changed = self._has_changed(strategy, atr_before, self._ATR_KEYS)
        adaptive_changed = self._has_changed(strategy, adaptive_before, self._ADAPTIVE_KEYS)
        if atr_changed:
            strategy._last_atr_value = None
            strategy._last_atr_time = 0.0
        if adaptive_changed:
            strategy._last_adapt_bar_time = 0.0

        enabled_changed = ("enabled" in cfg) and (bool(cfg["enabled"]) != before["enabled"])
        order_related_changed = self._has_changed(strategy, before, self._ORDER_RELATED_KEYS)
        should_clear_orders = enabled_changed or order_related_changed or atr_changed or adaptive_changed

        if should_clear_orders and not cleared_orders:
            strategy.clear_old_orders()

        strategy.set_state(current_state)
        Logger.log(
            "SYSTEM",
            "UPDATE",
            f"Strategy updated: {strategy.symbol} (Magic: {strategy.magic}, Enabled: {strategy.enabled}, "
            f"OrdersCleared: {should_clear_orders})",
        )

    @classmethod
    def _snapshot_order_related_state(cls, strategy: GridStrategy) -> dict:
        state = cls._snapshot(strategy, cls._ORDER_RELATED_KEYS)
        state["symbol"] = strategy.symbol
        state["enabled"] = strategy.enabled
        return state

    @staticmethod
    def _snapshot(strategy: GridStrategy, keys: tuple) -> dict:
        return {key: getattr(strategy, key, None) for key in keys}

    @staticmethod
    def _has_changed(strategy: GridStrategy, before: dict, keys: tuple) -> bool:
        return any(getattr(strategy, key, None) != before[key] for key in keys)

    @staticmethod
    def _apply_updates(strategy: GridStrategy, cfg: dict, keys: tuple, *, coerce_value=None):
        for key in keys:
            if key not in cfg:
                continue
            value = cfg[key]
            if coerce_value is not None:
                value = coerce_value(strategy, key, value)
            setattr(strategy, key, value)

    @staticmethod
    def _apply_base_updates(strategy: GridStrategy, cfg: dict):
        if "enabled" in cfg:
            strategy.enabled = bool(cfg["enabled"])

        if "step" in cfg and cfg.get("step") is not None:
            new_step = float(cfg["step"])
            if new_step != strategy.step:
                strategy.step = new_step
                strategy.base_step = new_step

        if "tp_dist" in cfg:
            strategy.tp_dist = cfg["tp_dist"]
            strategy.base_tp_dist = strategy.tp_dist
        if "sl_dist" in cfg and cfg.get("sl_dist") is not None:
            strategy.sl_dist = cfg["sl_dist"]
        if "lot" in cfg:
            strategy.lot = cfg["lot"]
            strategy.base_lot = strategy.lot

    @staticmethod
    def _apply_window_and_range_updates(strategy: GridStrategy, cfg: dict):
        window_changed = False
        if "window" in cfg and cfg.get("window") is not None:
            new_window = int(cfg["window"])
            if new_window != strategy.window:
                strategy.window = new_window
                window_changed = True

        if "min_p" in cfg and cfg.get("min_p") is not None:
            strategy.min_price = cfg["min_p"]
        if "max_p" in cfg and cfg.get("max_p") is not None:
            strategy.max_price = cfg["max_p"]

        if "buy_window" in cfg and cfg.get("buy_window") is not None:
            buy_window = cfg.get("buy_window")
            strategy.buy_window = int(buy_window) if buy_window is not None else strategy.window
        elif window_changed:
            strategy.buy_window = strategy.window

        if "sell_window" in cfg and cfg.get("sell_window") is not None:
            sell_window = cfg.get("sell_window")
            strategy.sell_window = int(sell_window) if sell_window is not None else strategy.window
        elif window_changed:
            strategy.sell_window = strategy.window

    @classmethod
    def _coerce_general_value(cls, strategy: GridStrategy, key: str, value):
        if key == "mode":
            return strategy._normalize_mode(value)
        return value


# ── Strategy manager ──────────────────────────────────────────────────────────

_ALLOWED_KEYS: Set[str] | None = None

def _get_allowed_keys() -> Set[str]:
    global _ALLOWED_KEYS
    if _ALLOWED_KEYS is None:
        params = inspect.signature(GridStrategy.__init__).parameters
        _ALLOWED_KEYS = {name for name in params if name != "self"}
    return _ALLOWED_KEYS

def build_strategy(cfg: Dict[str, Any], *, lock: Any = None, datafeed: Any = None) -> GridStrategy:
    normalized = normalize_config(cfg)
    allowed = _get_allowed_keys()
    kwargs = {key: normalized[key] for key in allowed if key in normalized}
    if lock is not None:
        kwargs["lock"] = lock
    if datafeed is not None:
        kwargs["datafeed"] = datafeed
    return GridStrategy(**kwargs)


class StrategyManager:
    def __init__(self, broker, config_loader, datafeed: DataFeed | None = None):
        self.broker = broker
        self.config_loader = config_loader
        self.active: Dict[int, GridStrategy] = {}
        self._updater = StrategyUpdater(broker)
        self.datafeed = datafeed or DataFeed(broker)

    def sync(self):
        configs = self._load_changed_configs()
        if configs is None:
            return

        new_magics = self._sync_configs(configs)
        self._remove_inactive_strategies(new_magics)

    def _load_changed_configs(self) -> list[dict] | None:
        changed, configs = self.config_loader.load_if_changed()
        if not changed:
            return None

        if configs is None:
            Logger.log("SYSTEM", "WARN", f"Config load returned None: {self.config_loader.config_path}")
            return None

        if not configs:
            Logger.log(
                "SYSTEM",
                "WARN",
                f"Config file is empty or has no valid strategies: {self.config_loader.config_path}",
            )
            return None

        return configs

    @staticmethod
    def _extract_magic(cfg: dict) -> int | None:
        magic = cfg.get("magic") if isinstance(cfg, dict) else None
        if magic is None:
            Logger.log("SYSTEM", "CONFIG_ERROR", "Config missing magic; skipping entry.")
            return None
        return magic

    def _sync_configs(self, configs: list[dict]) -> set[int]:
        new_magics: set[int] = set()
        for cfg in configs:
            magic = self._extract_magic(cfg)
            if magic is None:
                continue
            new_magics.add(magic)
            self._upsert_strategy(magic, cfg)
        return new_magics

    def _upsert_strategy(self, magic: int, cfg: dict):
        strategy = self.active.get(magic)
        if strategy is None:
            self._add_strategy(cfg)
            return
        self._updater.apply(strategy, cfg)

    def _remove_inactive_strategies(self, new_magics: set[int]):
        for magic in tuple(self.active):
            if magic not in new_magics:
                self._remove_strategy(magic)

    def _add_strategy(self, cfg: dict):
        Logger.log(
            "SYSTEM",
            "ADD",
            f"Add strategy {cfg.get('symbol')} (Magic: {cfg.get('magic')}, Enabled: {cfg.get('enabled', True)})",
        )
        symbol = cfg.get("symbol")
        if not self.broker.ensure_symbol(symbol):
            Logger.log("SYSTEM", "ERROR", f"Symbol unavailable: {symbol}")
            return
        strategy = build_strategy(cfg, lock=self.broker.lock, datafeed=self.datafeed)
        self.active[cfg["magic"]] = strategy
        strategy.clear_old_orders()

    def _remove_strategy(self, magic: int):
        strategy = self.active.pop(magic, None)
        if not strategy:
            return
        Logger.log("SYSTEM", "REMOVE", f"Remove strategy {strategy.symbol} (Magic: {magic})")
        strategy.clear_old_orders()
