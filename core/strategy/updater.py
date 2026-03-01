"""Apply config changes to active strategies."""

from __future__ import annotations

from core.logger import Logger
from core.strategy.grid_strategy.strategy import GridStrategy

from .config import normalize_config


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
        "extreme_mode",
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
            # Ensure pending orders reflect latest strategy parameters.
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
    def _snapshot(strategy: GridStrategy, keys: tuple[str, ...]) -> dict:
        return {key: getattr(strategy, key, None) for key in keys}

    @staticmethod
    def _has_changed(strategy: GridStrategy, before: dict, keys: tuple[str, ...]) -> bool:
        return any(getattr(strategy, key, None) != before[key] for key in keys)

    @staticmethod
    def _apply_updates(strategy: GridStrategy, cfg: dict, keys: tuple[str, ...], *, coerce_value=None):
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
