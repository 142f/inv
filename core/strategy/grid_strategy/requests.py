"""Helpers for MT5 request filling mode handling."""

from __future__ import annotations

import MetaTrader5 as mt5

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

