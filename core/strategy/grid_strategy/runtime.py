"""Runtime helpers shared by grid strategy mixins."""

from __future__ import annotations

import time

import MetaTrader5 as mt5

from .requests import apply_default_filling_mode

_MARKET_TICK_MAX_AGE_SECONDS = 600


class QueuedResult:
    def __init__(self):
        self.retcode = mt5.TRADE_RETCODE_DONE
        self.comment = "QUEUED"
        self.order = 0


class GridRuntimeMixin:
    def _mt5_call(self, func, *args, **kwargs):
        """Wrap an MT5 API call with the shared lock when available."""
        if self.lock:
            with self.lock:
                return func(*args, **kwargs)
        return func(*args, **kwargs)

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

