"""Apply config changes to active strategies."""

from __future__ import annotations

from core.logger import Logger
from core.strategy_lib import GridStrategy

from .config import normalize_config


class StrategyUpdater:
    def __init__(self, broker):
        self.broker = broker

    def apply(self, strategy: GridStrategy, cfg: dict):
        cfg = normalize_config(cfg)
        current_state = strategy.get_state()

        new_symbol = cfg.get("symbol", strategy.symbol)
        if strategy.symbol != new_symbol:
            self.broker.ensure_symbol(new_symbol)
            strategy.set_symbol(new_symbol, reset_runtime_state=True)
            current_state = {}
            Logger.log("SYSTEM", "UPDATE", f"Strategy {strategy.magic} symbol -> {strategy.symbol}")

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
        if "lot" in cfg:
            strategy.lot = cfg["lot"]

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
            bw = cfg.get("buy_window")
            strategy.buy_window = int(bw) if bw is not None else strategy.window
        elif window_changed:
            strategy.buy_window = strategy.window

        if "sell_window" in cfg and cfg.get("sell_window") is not None:
            sw = cfg.get("sell_window")
            strategy.sell_window = int(sw) if sw is not None else strategy.window
        elif window_changed:
            strategy.sell_window = strategy.window

        for key in ("use_atr", "use_atr_tp", "atr_period", "atr_factor", "atr_tp_factor", "atr_mode", "atr_timeframe"):
            if key in cfg:
                setattr(strategy, key, cfg[key])

        for key in ("atr_update_seconds", "atr_smooth", "atr_change_threshold", "min_step_mult", "max_step_mult"):
            if key in cfg:
                setattr(strategy, key, cfg[key])

        for key in (
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
        ):
            if key in cfg:
                setattr(strategy, key, cfg[key])

        for key in (
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
        ):
            if key in cfg:
                setattr(strategy, key, cfg[key])

        # Ensure pending orders reflect latest strategy parameters.
        strategy.clear_old_orders()

        strategy.set_state(current_state)
        Logger.log("SYSTEM", "UPDATE", f"Strategy updated: {strategy.symbol} (Enabled: {strategy.enabled})")
