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
        current_state.pop("enabled", None)
        cleared_orders = False

        before = {
            "symbol": strategy.symbol,
            "enabled": strategy.enabled,
            "step": strategy.step,
            "tp_dist": strategy.tp_dist,
            "sl_dist": strategy.sl_dist,
            "lot": strategy.lot,
            "window": strategy.window,
            "buy_window": strategy.buy_window,
            "sell_window": strategy.sell_window,
            "min_price": strategy.min_price,
            "max_price": strategy.max_price,
            "mode": strategy.mode,
            "auto_trim": getattr(strategy, "auto_trim", False),
        }

        atr_keys = (
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
        adaptive_keys = (
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
        atr_before = {key: getattr(strategy, key, None) for key in atr_keys}
        adaptive_before = {key: getattr(strategy, key, None) for key in adaptive_keys}

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

        for key in ("use_atr", "atr_period", "atr_factor", "atr_mode", "atr_timeframe"):
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
            "auto_trim",
        ):
            if key in cfg:
                value = cfg[key]
                if key == "mode":
                    value = strategy._normalize_mode(value)
                # 对数值类型字段进行类型转换，防止字符串类型导致比较错误
                elif key in ("recenter_steps", "max_long_pos", "max_short_pos", "max_new_orders_per_update"):
                    value = int(value) if value is not None else None
                elif key in ("recenter_cooldown", "max_long_vol", "max_short_vol", "max_net_vol",
                             "max_spread_points", "extreme_cooldown"):
                    value = float(value) if value is not None else None
                setattr(strategy, key, value)

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

        atr_changed = any(getattr(strategy, key, None) != atr_before[key] for key in atr_keys)
        adaptive_changed = any(getattr(strategy, key, None) != adaptive_before[key] for key in adaptive_keys)
        if atr_changed:
            strategy._last_atr_value = None
            strategy._last_atr_time = 0.0
        if adaptive_changed:
            strategy._last_adapt_bar_time = 0.0

        enabled_changed = ("enabled" in cfg) and (bool(cfg["enabled"]) != before["enabled"])
        order_related_changed = any(
            (
                strategy.step != before["step"],
                strategy.tp_dist != before["tp_dist"],
                strategy.sl_dist != before["sl_dist"],
                strategy.lot != before["lot"],
                strategy.window != before["window"],
                strategy.buy_window != before["buy_window"],
                strategy.sell_window != before["sell_window"],
                strategy.min_price != before["min_price"],
                strategy.max_price != before["max_price"],
                strategy.mode != before["mode"],
                strategy.auto_trim != before["auto_trim"],
            )
        )
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
