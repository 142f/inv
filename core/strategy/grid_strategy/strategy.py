"""GridStrategy assembly and initialization."""

from __future__ import annotations

import time
import MetaTrader5 as mt5

from core.logger import Logger
from core.strategy.grid import InventoryWindowPolicy, RelativeSpreadFusePolicy, UtilityOrderSelector
from core.strategy.components import GridCalculator, RiskManager

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

VALID_MODES = {"neutral", "long", "short"}

def as_optional(value, cast):
    return cast(value) if value is not None else None

def build_stats_state(magic: int) -> dict:
    return {
        "magic": magic,
        "start_time": time.time(),
        "last_reset_time": time.time(),
        "long_profitable_count": 0,
        "long_profitable_amount": 0.0,
        "short_profitable_count": 0,
        "short_profitable_amount": 0.0,
        "last_stats_update_time": 0,
    }

from .adaptive import GridAdaptiveMixin
from .hedge import GridHedgeMixin
from .orders import GridOrdersMixin
from .runtime_mixins import (
    GridRuntimeMixin,
    GridStateStatsMixin,
    GridSymbolMixin,
)
from .update import GridUpdateMixin


class GridStrategy(
    GridRuntimeMixin,
    GridStateStatsMixin,
    GridSymbolMixin,
    GridAdaptiveMixin,
    GridOrdersMixin,
    GridHedgeMixin,
    GridUpdateMixin,
):
    _TIMEFRAME_MAP = TIMEFRAME_MAP
    _VALID_MODES = VALID_MODES

    def __init__(
        self,
        symbol,
        step,
        tp_dist,
        lot,
        magic,
        sl_dist=0,
        window=6,
        min_p=0,
        max_p=999999,
        enabled=True,
        use_atr=False,
        atr_period=14,
        atr_factor=1.0,
        atr_mode="wilder",
        atr_timeframe="M15",
        adaptive_enabled=False,
        adaptive_timeframe="M15",
        adaptive_lookback=200,
        adaptive_quantile_low=0.30,
        adaptive_quantile_high=0.70,
        adaptive_step_mult_low=0.90,
        adaptive_step_mult_high=1.10,
        adaptive_lot_min_mult=0.50,
        adaptive_lot_max_mult=1.50,
        adaptive_range_buffer_atr=1.0,
        mode="neutral",
        buy_window=None,
        sell_window=None,
        out_of_range_action="freeze",
        atr_update_seconds=5,
        atr_smooth=0.1,
        atr_change_threshold=0.01,
        min_step_mult=0.5,
        max_step_mult=3.0,
        auto_trim=False,
        lock=None,
        datafeed=None,
        anchor=None,
        recenter_steps=3,
        recenter_cooldown=30,
        max_long_pos=None,
        max_short_pos=None,
        max_long_vol=None,
        max_short_vol=None,
        max_net_vol=None,
        max_spread_points=None,
        extreme_cooldown=30,
        max_new_orders_per_update=10,
        hedge_enabled=False,
        hedge_fraction=0.3333,
        hedge_tranches=3,
        hedge_entry_steps=1,
        hedge_exit_steps=1,
        hedge_cooldown=20,
        max_gross_vol=None,
        hedge_vol_lookback=300,
        hedge_vol_window=20,
        hedge_vol_quantile=0.90,
        hedge_vol_base=200,
        hedge_vol_mult=3.0,
        be_trigger_steps=1,
        be_buffer_points=20,
    ):
        self.symbol = symbol

        self._init_base_params(
            step=step,
            tp_dist=tp_dist,
            sl_dist=sl_dist,
            lot=lot,
            magic=magic,
            window=window,
            min_p=min_p,
            max_p=max_p,
            enabled=enabled,
            use_atr=use_atr,
            atr_period=atr_period,
            atr_factor=atr_factor,
            atr_mode=atr_mode,
            atr_timeframe=atr_timeframe,
        )
        self._init_adaptive_params(
            adaptive_enabled=adaptive_enabled,
            adaptive_timeframe=adaptive_timeframe,
            adaptive_lookback=adaptive_lookback,
            adaptive_quantile_low=adaptive_quantile_low,
            adaptive_quantile_high=adaptive_quantile_high,
            adaptive_step_mult_low=adaptive_step_mult_low,
            adaptive_step_mult_high=adaptive_step_mult_high,
            adaptive_lot_min_mult=adaptive_lot_min_mult,
            adaptive_lot_max_mult=adaptive_lot_max_mult,
            adaptive_range_buffer_atr=adaptive_range_buffer_atr,
        )
        self._init_mode_and_atr_updates(
            mode=mode,
            buy_window=buy_window,
            sell_window=sell_window,
            window=window,
            out_of_range_action=out_of_range_action,
            atr_update_seconds=atr_update_seconds,
            atr_smooth=atr_smooth,
            atr_change_threshold=atr_change_threshold,
            min_step_mult=min_step_mult,
            max_step_mult=max_step_mult,
            auto_trim=auto_trim,
        )
        self._init_runtime_dependencies(lock=lock, datafeed=datafeed)
        self._init_anchor_and_caps(
            anchor=anchor,
            recenter_steps=recenter_steps,
            recenter_cooldown=recenter_cooldown,
            max_long_pos=max_long_pos,
            max_short_pos=max_short_pos,
            max_long_vol=max_long_vol,
            max_short_vol=max_short_vol,
            max_net_vol=max_net_vol,
            max_spread_points=max_spread_points,
            extreme_cooldown=extreme_cooldown,
            max_new_orders_per_update=max_new_orders_per_update,
        )
        self._init_hedge_params(
            hedge_enabled=hedge_enabled,
            hedge_fraction=hedge_fraction,
            hedge_tranches=hedge_tranches,
            hedge_entry_steps=hedge_entry_steps,
            hedge_exit_steps=hedge_exit_steps,
            hedge_cooldown=hedge_cooldown,
            max_gross_vol=max_gross_vol,
            hedge_vol_lookback=hedge_vol_lookback,
            hedge_vol_window=hedge_vol_window,
            hedge_vol_quantile=hedge_vol_quantile,
            hedge_vol_base=hedge_vol_base,
            hedge_vol_mult=hedge_vol_mult,
            be_trigger_steps=be_trigger_steps,
            be_buffer_points=be_buffer_points,
        )
        self._init_runtime_state()
        self._cache_symbol_info()

    def _init_base_params(
        self,
        *,
        step,
        tp_dist,
        sl_dist,
        lot,
        magic,
        window,
        min_p,
        max_p,
        enabled,
        use_atr,
        atr_period,
        atr_factor,
        atr_mode,
        atr_timeframe,
    ):
        self.base_step = float(step)
        self.step = float(step)
        self.base_tp_dist = float(tp_dist)
        self.tp_dist = float(tp_dist)
        self.sl_dist = float(sl_dist)
        self.base_lot = float(lot)
        self.lot = float(lot)
        self.magic = int(magic)
        self.window = int(window)
        self.min_price = float(min_p)
        self.max_price = float(max_p)
        # [修复 L-06] 保留用户配置的原始硬边界，供 adaptive 动态范围更新时做 clamp 用。
        self._user_min_price = float(min_p)
        self._user_max_price = float(max_p)
        self.enabled = enabled
        self.pause_until = 0
        self.use_atr = use_atr
        self.atr_period = atr_period
        self.atr_factor = atr_factor
        self.atr_mode = atr_mode
        self.atr_timeframe = atr_timeframe

    def _init_adaptive_params(
        self,
        *,
        adaptive_enabled,
        adaptive_timeframe,
        adaptive_lookback,
        adaptive_quantile_low,
        adaptive_quantile_high,
        adaptive_step_mult_low,
        adaptive_step_mult_high,
        adaptive_lot_min_mult,
        adaptive_lot_max_mult,
        adaptive_range_buffer_atr,
    ):
        self.adaptive_enabled = adaptive_enabled
        self.adaptive_timeframe = adaptive_timeframe
        self.adaptive_lookback = int(adaptive_lookback)
        self.adaptive_quantile_low = float(adaptive_quantile_low)
        self.adaptive_quantile_high = float(adaptive_quantile_high)
        self.adaptive_step_mult_low = float(adaptive_step_mult_low)
        self.adaptive_step_mult_high = float(adaptive_step_mult_high)
        self.adaptive_lot_min_mult = float(adaptive_lot_min_mult)
        self.adaptive_lot_max_mult = float(adaptive_lot_max_mult)
        self.adaptive_range_buffer_atr = float(adaptive_range_buffer_atr)

    def _init_mode_and_atr_updates(
        self,
        *,
        mode,
        buy_window,
        sell_window,
        window,
        out_of_range_action,
        atr_update_seconds,
        atr_smooth,
        atr_change_threshold,
        min_step_mult,
        max_step_mult,
        auto_trim,
    ):
        self.mode = self._normalize_mode(mode)
        self.buy_window = buy_window if buy_window is not None else window
        self.sell_window = sell_window if sell_window is not None else window
        self.out_of_range_action = out_of_range_action
        self.atr_update_seconds = atr_update_seconds
        self.atr_smooth = atr_smooth
        self.atr_change_threshold = atr_change_threshold
        self.min_step_mult = min_step_mult
        self.max_step_mult = max_step_mult
        self.auto_trim = bool(auto_trim)

    def _init_runtime_dependencies(self, *, lock, datafeed):
        self.lock = lock
        self.datafeed = datafeed
        self.bid_orders = {}
        self.ask_orders = {}
        self._action_collector = None
        self.grid_calculator = GridCalculator(self._normalize_price)
        self.risk_manager = RiskManager()
        self._spread_fuse_policy = RelativeSpreadFusePolicy()
        self._inventory_window_policy = InventoryWindowPolicy()
        self._order_selector = UtilityOrderSelector()

    def _init_anchor_and_caps(
        self,
        *,
        anchor,
        recenter_steps,
        recenter_cooldown,
        max_long_pos,
        max_short_pos,
        max_long_vol,
        max_short_vol,
        max_net_vol,
        max_spread_points,
        extreme_cooldown,
        max_new_orders_per_update,
    ):
        self.anchor = as_optional(anchor, float)
        self.recenter_steps = int(recenter_steps)
        self.recenter_cooldown = float(recenter_cooldown)
        self._last_recenter_time = 0
        self.max_long_pos = as_optional(max_long_pos, int)
        self.max_short_pos = as_optional(max_short_pos, int)
        self.max_long_vol = as_optional(max_long_vol, float)
        self.max_short_vol = as_optional(max_short_vol, float)
        self.max_net_vol = as_optional(max_net_vol, float)
        self.max_spread_points = as_optional(max_spread_points, float)
        self.extreme_cooldown = float(extreme_cooldown)
        self.max_new_orders_per_update = int(max_new_orders_per_update)

    def _init_hedge_params(
        self,
        *,
        hedge_enabled,
        hedge_fraction,
        hedge_tranches,
        hedge_entry_steps,
        hedge_exit_steps,
        hedge_cooldown,
        max_gross_vol,
        hedge_vol_lookback,
        hedge_vol_window,
        hedge_vol_quantile,
        hedge_vol_base,
        hedge_vol_mult,
        be_trigger_steps,
        be_buffer_points,
    ):
        self.hedge_enabled = bool(hedge_enabled)
        self.hedge_fraction = float(hedge_fraction)
        self.hedge_tranches = int(hedge_tranches)
        self.hedge_entry_steps = int(hedge_entry_steps)
        self.hedge_exit_steps = int(hedge_exit_steps)
        self.hedge_cooldown = float(hedge_cooldown)
        self.max_gross_vol = as_optional(max_gross_vol, float)
        self.hedge_vol_lookback = int(hedge_vol_lookback)
        self.hedge_vol_window = int(hedge_vol_window)
        self.hedge_vol_quantile = float(hedge_vol_quantile)
        self.hedge_vol_base = int(hedge_vol_base)
        self.hedge_vol_mult = float(hedge_vol_mult)
        self.be_trigger_steps = int(be_trigger_steps)
        self.be_buffer_points = int(be_buffer_points)
        self._last_hedge_time = 0.0
        self._last_hedge_entry_price = None

    def _init_runtime_state(self):
        self._last_atr_value = None
        self._last_atr_time = 0
        self._last_adapt_bar_time = 0
        self._adaptive_step_mult_state = 1.0
        self._adaptive_lot_mult_state = 1.0
        self._last_status_log_time = 0
        self._status_log_interval = 60
        self._spread_fuse_active = False
        self._spread_rel_atr_enter = 0.35
        self._spread_rel_mid_enter = 0.0030
        self._spread_rel_atr_exit = 0.25
        self._spread_rel_mid_exit = 0.0020
        self._spread_fuse_hold_seconds = max(2.0, min(10.0, self.extreme_cooldown * 0.25))
        self._stats = build_stats_state(self.magic)
        self._order_profit = {}
        self._order_type = {}
        self._last_deal_time = self._stats["last_reset_time"]
        self._last_deal_ticket = 0
        # [P-12] _index_orders 增量更新所需的上次 ticket 集合快照
        self._last_order_tickets = None

    def _normalize_mode(self, mode):
        normalized = str(mode or "neutral").strip().lower()
        if normalized not in self._VALID_MODES:
            Logger.log(self.symbol, "WARN", f"Invalid mode '{mode}', fallback to neutral")
            return "neutral"
        return normalized

