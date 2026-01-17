"""Grid target calculator."""

from __future__ import annotations

from typing import Callable, List, Tuple


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
    ) -> Tuple[List[float], List[float]]:
        target_buys: List[float] = []
        target_sells: List[float] = []

        if step <= 0:
            return target_buys, target_sells

        search_range_buy = buy_window + recenter_steps + 5
        search_range_sell = sell_window + recenter_steps + 5

        mid_price = (bid + ask) / 2.0
        if not (min_price <= mid_price <= max_price):
            return target_buys, target_sells

        if mode in ["neutral", "long"]:
            for i in range(0, search_range_buy):
                level = self._normalize_price(anchor - (i * step))
                if level < ask and level >= min_price:
                    target_buys.append(level)
            target_buys = target_buys[:buy_window]

        if mode in ["neutral", "short"]:
            for i in range(0, search_range_sell):
                level = self._normalize_price(anchor + (i * step))
                if level > bid and level <= max_price:
                    target_sells.append(level)
            target_sells = target_sells[:sell_window]
            target_sells.sort(reverse=True)

        return target_buys, target_sells
