"""Grid target calculator."""

from __future__ import annotations

from typing import Callable, List, Optional, Set, Tuple


class GridCalculator:
    def __init__(self, normalize_price: Callable[[float], float]):
        self._normalize_price = normalize_price

    def build_targets(
        self,
        *,
        anchor: float,
        step: float,
        min_price: float,
        max_price: float,
        bid: float,
        ask: float,
        buy_window: int,
        sell_window: int,
        mode: str,
        recenter_steps: int,
        min_dist: Optional[float] = None,
        blocked_k: Optional[Set[int]] = None,
    ) -> Tuple[List[float], List[float]]:
        target_buys: List[float] = []
        target_sells: List[float] = []

        if step <= 0:
            return target_buys, target_sells

        blocked_k = blocked_k or set()
        min_dist = max(0.0, float(min_dist)) if min_dist is not None else 0.0

        mid_price = (bid + ask) / 2.0
        if not (min_price <= mid_price <= max_price):
            return target_buys, target_sells

        def _should_skip(level: float, side: str) -> bool:
            if min_dist > 0.0:
                if side == "buy" and (ask - level) < min_dist:
                    return True
                if side == "sell" and (level - bid) < min_dist:
                    return True
            if blocked_k:
                k = round((level - anchor) / step)
                if k in blocked_k:
                    return True
            return False

        if mode in ["neutral", "long"] and buy_window > 0:
            seen_buys = set()
            max_i = int((anchor - min_price) / step) if anchor > min_price else 0
            i = 0
            while i <= max_i and len(target_buys) < buy_window:
                level = self._normalize_price(anchor - (i * step))
                if level < ask and level >= min_price:
                    if level not in seen_buys and not _should_skip(level, "buy"):
                        target_buys.append(level)
                        seen_buys.add(level)
                i += 1

        if mode in ["neutral", "short"] and sell_window > 0:
            seen_sells = set()
            max_i = int((max_price - anchor) / step) if max_price > anchor else 0
            i = 0
            while i <= max_i and len(target_sells) < sell_window:
                level = self._normalize_price(anchor + (i * step))
                if level > bid and level <= max_price:
                    if level not in seen_sells and not _should_skip(level, "sell"):
                        target_sells.append(level)
                        seen_sells.add(level)
                i += 1
            target_sells.sort(reverse=True)

        return target_buys, target_sells
