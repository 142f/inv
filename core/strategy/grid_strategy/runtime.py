"""Runtime helpers shared by grid strategy mixins."""

from __future__ import annotations

import time

import MetaTrader5 as mt5

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


class GridRuntimeMixin:
    def _mt5_call(self, func, *args, **kwargs):
        """Wrap an MT5 API call with the shared lock when available."""
        def _execute():
            # 对于 mt5.order_check 和 mt5.order_send，需要直接传递字典参数
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