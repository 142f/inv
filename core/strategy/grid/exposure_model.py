from __future__ import annotations

import math
from typing import Any, Iterable

import MetaTrader5 as mt5


def estimate_fill_probability(
    *,
    side: str,
    price: float,
    bid: float,
    ask: float,
    point: float,
    step: float,
    atr: float | None = None,
) -> float:
    """Estimate pending-order fill probability using distance/volatility/spread."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0

    side_norm = str(side).lower().strip()
    if side_norm not in {"buy", "sell"}:
        return 0.0

    distance = (ask - price) if side_norm == "buy" else (price - bid)
    distance = max(0.0, float(distance))
    spread = max(0.0, float(ask - bid))
    safe_point = max(float(point or 0.0), 1e-9)
    base_step = max(float(step or 0.0), safe_point * 10.0)
    atr_scale = max(0.0, float(atr or 0.0)) * 0.5
    scale = max(base_step, atr_scale, spread * 4.0, safe_point)

    prob = math.exp(-distance / max(scale, 1e-9))
    spread_points = spread / safe_point
    prob = prob / (1.0 + 0.05 * spread_points)
    return float(max(0.01, min(0.995, prob)))


def calc_predicted_net_exposure(
    *,
    positions: Iterable[Any],
    orders: Iterable[Any],
    tick: Any,
    point: float,
    step: float,
    atr: float | None = None,
) -> float:
    """Predicted net exposure with probabilistic pending-order contribution."""
    net_vol = 0.0
    for p in positions:
        p_type = getattr(p, "type", None)
        p_vol = float(getattr(p, "volume", 0.0) or 0.0)
        if p_type == mt5.POSITION_TYPE_BUY:
            net_vol += p_vol
        elif p_type == mt5.POSITION_TYPE_SELL:
            net_vol -= p_vol

    tick_valid = (
        tick is not None
        and float(getattr(tick, "bid", 0.0) or 0.0) > 0
        and float(getattr(tick, "ask", 0.0) or 0.0) > 0
        and float(getattr(tick, "ask", 0.0) or 0.0) >= float(getattr(tick, "bid", 0.0) or 0.0)
    )

    if not tick_valid:
        for o in orders:
            vol = float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.0)) or 0.0)
            o_type = getattr(o, "type", None)
            if o_type == mt5.ORDER_TYPE_BUY_LIMIT:
                net_vol += vol
            elif o_type == mt5.ORDER_TYPE_SELL_LIMIT:
                net_vol -= vol
        return net_vol

    bid = float(tick.bid)
    ask = float(tick.ask)
    for o in orders:
        vol = float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.0)) or 0.0)
        if vol <= 0:
            continue

        o_type = getattr(o, "type", None)
        price_open = float(getattr(o, "price_open", 0.0) or 0.0)
        if o_type == mt5.ORDER_TYPE_BUY_LIMIT:
            p_fill = estimate_fill_probability(
                side="buy",
                price=price_open,
                bid=bid,
                ask=ask,
                point=point,
                step=step,
                atr=atr,
            )
            net_vol += vol * p_fill
        elif o_type == mt5.ORDER_TYPE_SELL_LIMIT:
            p_fill = estimate_fill_probability(
                side="sell",
                price=price_open,
                bid=bid,
                ask=ask,
                point=point,
                step=step,
                atr=atr,
            )
            net_vol -= vol * p_fill
    return net_vol

