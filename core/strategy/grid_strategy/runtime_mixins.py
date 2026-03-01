"""Runtime mixins for grid strategy: base MT5 calls, symbol info, and state/stats."""

from __future__ import annotations

import time

import MetaTrader5 as mt5

from core.logger import Logger

# ---------------------------------------------------------------------------
# Module-level helpers (originally in runtime.py)
# ---------------------------------------------------------------------------

_MARKET_TICK_MAX_AGE_SECONDS = 600

ALLOWED_FILLING_MODES = (
    mt5.ORDER_FILLING_FOK,
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_RETURN,
)

FILLING_FALLBACK_ORDER = (
    mt5.ORDER_FILLING_RETURN,
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_FOK,
)


def apply_default_filling_mode(request, filling_mode):
    if request is None:
        return None
    if not isinstance(request, dict):
        return request
    action = request.get("action")
    if action not in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING):
        return request
    if "type_filling" in request:
        return request
    if filling_mode in ALLOWED_FILLING_MODES:
        req = dict(request)
        req["type_filling"] = filling_mode
        return req
    return request


def iter_filling_candidates(default_mode):
    seen = set()
    if default_mode in ALLOWED_FILLING_MODES:
        seen.add(default_mode)
        yield default_mode
    for mode in FILLING_FALLBACK_ORDER:
        if mode in seen:
            continue
        seen.add(mode)
        yield mode


class QueuedResult:
    def __init__(self):
        self.retcode = mt5.TRADE_RETCODE_DONE
        self.comment = "QUEUED"
        self.order = 0


# ---------------------------------------------------------------------------
# GridRuntimeMixin (originally in runtime.py)
# ---------------------------------------------------------------------------

class GridRuntimeMixin:
    def _mt5_call(self, func, *args, **kwargs):
        """Wrap an MT5 API call with the shared lock when available."""
        def _execute():
            # order_check and order_send require a plain dict argument
            if func in (mt5.order_check, mt5.order_send) and args and isinstance(args[0], dict):
                return func(args[0])
            return func(*args, **kwargs)

        if self.lock:
            with self.lock:
                return _execute()
        return _execute()

    def _get_tick(self):
        return self._mt5_call(mt5.symbol_info_tick, self.symbol)

    def _is_market_open(self, tick=None):
        if tick is None:
            tick = self._get_tick()
        if not tick:
            return False
        if abs(time.time() - tick.time) > _MARKET_TICK_MAX_AGE_SECONDS:
            return False
        return True

    def _prepare_request(self, request):
        return apply_default_filling_mode(request, self.filling_mode)

    def _append_action_if_queued(self, request):
        if self._action_collector is None:
            return False
        if request is not None:
            self._action_collector.append(request)
        return True

    def _queue_action(self, request):
        request = self._prepare_request(request)
        return self._append_action_if_queued(request)

    def _dispatch_request(self, request):
        request = self._prepare_request(request)
        if self._append_action_if_queued(request):
            return QueuedResult()
        return self._mt5_call(mt5.order_send, request)


# ---------------------------------------------------------------------------
# GridStateStatsMixin (originally in state_stats.py)
# ---------------------------------------------------------------------------

class GridStateStatsMixin:
    def get_state(self):
        """Return strategy internal state for config-sync persistence."""
        return {
            'pause_until': self.pause_until,
            'enabled': self.enabled,
            '_last_atr_value': self._last_atr_value,
            '_last_tick_time': self._last_tick_time,
            '_last_atr_time': self._last_atr_time,
            'anchor': self.anchor,
            '_last_recenter_time': self._last_recenter_time,
            "_last_hedge_time": self._last_hedge_time,
            "_last_hedge_entry_price": self._last_hedge_entry_price,
            '_stats': self._stats,
        }

    def set_state(self, state):
        """Restore strategy internal state."""
        if state:
            self.pause_until = state.get('pause_until', self.pause_until)
            self.enabled = state.get('enabled', self.enabled)
            self._last_atr_value = state.get('_last_atr_value', self._last_atr_value)
            self._last_tick_time = state.get('_last_tick_time', self._last_tick_time)
            self._last_atr_time = state.get('_last_atr_time', self._last_atr_time)
            self.anchor = state.get('anchor', self.anchor)
            self._last_recenter_time = state.get('_last_recenter_time', self._last_recenter_time)
            self._last_hedge_time = float(state.get("_last_hedge_time", self._last_hedge_time) or 0.0)
            self._last_hedge_entry_price = state.get("_last_hedge_entry_price", self._last_hedge_entry_price)
            # Restore stats data
            if '_stats' in state:
                self._stats = state['_stats']

    def _deal_net_profit(self, deal) -> float:
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        return profit + swap + commission

    def _deal_time_value(self, deal) -> float:
        t = getattr(deal, "time", 0)
        if hasattr(t, "timestamp"):
            try:
                return float(t.timestamp())
            except Exception:
                pass
        try:
            return float(t)
        except Exception:
            return 0.0

    def _adjust_profitable_stats(self, order_type: str, amount_delta: float, count_delta: int = 0) -> None:
        if order_type == "long":
            self._stats['long_profitable_amount'] += amount_delta
            self._stats['long_profitable_count'] += count_delta
        elif order_type == "short":
            self._stats['short_profitable_amount'] += amount_delta
            self._stats['short_profitable_count'] += count_delta

    def _apply_deal_to_stats(self, deal) -> None:
        order_ticket = getattr(deal, "order", None)
        if order_ticket is None:
            return

        delta = self._deal_net_profit(deal)
        prev_total = float(self._order_profit.get(order_ticket, 0.0) or 0.0)

        order_type = self._order_type.get(order_ticket)
        type_was_known = order_type is not None
        if order_type is None:
            if deal.type == mt5.DEAL_TYPE_BUY:
                order_type = "long"
            elif deal.type == mt5.DEAL_TYPE_SELL:
                order_type = "short"
            if order_type:
                self._order_type[order_ticket] = order_type

        was_positive = prev_total > 0.0
        if not type_was_known:
            was_positive = False

        new_total = prev_total + delta
        self._order_profit[order_ticket] = new_total
        is_positive = new_total > 0.0

        if not order_type:
            return

        if was_positive:
            if is_positive:
                self._adjust_profitable_stats(order_type, delta, 0)
            else:
                self._adjust_profitable_stats(order_type, -prev_total, -1)
        else:
            if is_positive:
                self._adjust_profitable_stats(order_type, new_total, 1)

    def _update_stats(self):
        """Update stats incrementally from new deals."""
        now = time.time()
        # Limit update frequency to avoid heavy MT5 calls.
        if now - self._stats['last_stats_update_time'] < 300:  # 5 min
            return

        try:
            start_time = self._last_deal_time if self._last_deal_time else self._stats['last_reset_time']
            deals = self._mt5_call(mt5.history_deals_get, symbol=self.symbol, group="*", start=start_time)

            if deals:
                max_time = float(self._last_deal_time or 0.0)
                max_ticket = int(self._last_deal_ticket or 0)
                for deal in deals:
                    if deal.magic != self.magic:
                        continue

                    deal_time = self._deal_time_value(deal)
                    deal_ticket = int(getattr(deal, "ticket", 0) or 0)
                    if (deal_time < self._last_deal_time) or (
                        deal_time == self._last_deal_time and deal_ticket <= self._last_deal_ticket
                    ):
                        continue

                    self._apply_deal_to_stats(deal)

                    if (deal_time > max_time) or (deal_time == max_time and deal_ticket > max_ticket):
                        max_time = deal_time
                        max_ticket = deal_ticket

                self._last_deal_time = max_time
                self._last_deal_ticket = max_ticket

            self._stats['last_stats_update_time'] = now

        except Exception as e:
            Logger.log(self.symbol, "EXCEPTION", f"_update_stats error: {str(e)}")


# ---------------------------------------------------------------------------
# GridSymbolMixin (originally in symbol_runtime.py)
# ---------------------------------------------------------------------------

class GridSymbolMixin:
    def set_symbol(self, new_symbol: str, *, reset_runtime_state: bool = True):
        """Switch trading symbol and refresh symbol info cache.

        :param reset_runtime_state: When True, clears anchor, ATR cache,
            and hedge runtime state to avoid cross-symbol contamination.
        """
        if not new_symbol or new_symbol == self.symbol:
            return

        self.symbol = new_symbol
        # Refresh static symbol info cache
        self._cache_symbol_info()

        if reset_runtime_state:
            self.anchor = None
            self._last_recenter_time = 0.0
            self.pause_until = 0.0

            # ATR cache
            self._last_atr_value = None
            self._last_atr_time = 0.0
            self._last_adapt_bar_time = 0.0

            # Hedge runtime state
            self._last_hedge_time = 0.0
            self._last_hedge_entry_price = None

    @staticmethod
    def _precision_from_step(step: float) -> int:
        if step <= 0:
            return 2
        text = f"{step:.10f}".rstrip("0").rstrip(".")
        if "." in text:
            return len(text.split(".")[1])
        return 0

    def _cache_symbol_info(self):
        info = self._mt5_call(mt5.symbol_info, self.symbol)

        if info:
            self.digits = info.digits
            self.point = info.point
            self.stop_level = info.trade_stops_level * info.point
            self.vol_min = info.volume_min
            self.vol_max = info.volume_max
            self.vol_step = info.volume_step
            self.vol_precision = self._precision_from_step(self.vol_step)
            self.filling_mode = getattr(info, "filling_mode", None)
            self.initialized = True
        else:
            self.digits = 2
            self.point = 0.01
            self.stop_level = 0
            self.vol_min = 0.01
            self.vol_max = 100
            self.vol_step = 0.01
            self.vol_precision = 2
            self.filling_mode = None
            self.initialized = False
            Logger.log(self.symbol, "WARN", "Failed to fetch symbol info, using defaults.")

    def _normalize_price(self, price):
        return float(round(price, self.digits))

    def _normalize_volume(self, vol):
        if self.vol_step > 0:
            steps = round(vol / self.vol_step)
            vol = steps * self.vol_step
        precision = getattr(self, "vol_precision", 2)
        return float(round(max(self.vol_min, min(self.vol_max, vol)), precision))

    def _get_grid_level(self, price, anchor):
        """Snap price to the nearest grid line relative to anchor."""
        if self.step <= 0:
            return price
        k = round((price - anchor) / self.step)
        return anchor + k * self.step

    def _init_anchor_if_needed(self, mid_price):
        if self.anchor is None:
            if self.step <= 0:
                self.anchor = self._normalize_price(mid_price)
                return
            base0 = round(mid_price / self.step) * self.step
            self.anchor = self._normalize_price(base0)
            Logger.log(self.symbol, "INIT", f"Anchor Initialized | Price={self.anchor:.{self.digits}f}")

    def _maybe_recenter(self, mid_price):
        """Trigger recenter when drift >= recenter_steps*step and cooldown elapsed."""
        if self.step <= 0 or self.anchor is None:
            return False
        now = time.time()
        if now - self._last_recenter_time < self.recenter_cooldown:
            return False

        drift_steps = (mid_price - self.anchor) / self.step
        if abs(drift_steps) < self.recenter_steps:
            return False

        new_anchor = self._get_grid_level(mid_price, self.anchor)
        self.anchor = self._normalize_price(new_anchor)
        self._last_recenter_time = now
        Logger.log(self.symbol, "RECENTER", f"Anchor Shifted | New={self.anchor:.{self.digits}f} MidPrice={mid_price:.{self.digits}f}")
        return True
