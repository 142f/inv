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
        # If price is way out of bounds, maybe returning empty is correct, but let's be lenient
        # if not (min_price <= mid_price <= max_price):
        #    return target_buys, target_sells

        def _should_skip(level: float, side: str) -> bool:
            if min_dist > 0.0:
                if side == "buy" and (ask - level) < min_dist:
                    return True
                if side == "sell" and (level - bid) < min_dist:
                    return True
            if blocked_k:
                # Calculate k relative to the FIXED anchor (which is passed in)
                k = round((level - anchor) / step)
                if k in blocked_k:
                    return True
            return False

        # --- Fixed Grid + Sliding Window Logic ---
        
        # 1. Buy Orders (Below current price)
        # We want grid lines that are < Ask
        if mode in ["neutral", "long"] and buy_window > 0:
            start_k = int((ask - anchor) / step)
            # Check a bit higher just in case floating point issues
            start_k += 2 
            
            count = 0
            k = start_k
            
            # Scan downwards
            while count < buy_window:
                if (anchor + k * step) < (min_price - step): # Stop if we go below min_price
                     break
                     
                level = self._normalize_price(anchor + k * step)
                
                # Check strict price conditions
                if level < ask and level >= min_price and level <= max_price: 
                    # Only add if it satisfies min_dist and not blocked
                    if not _should_skip(level, "buy"):
                        target_buys.append(level)
                        count += 1
                
                k -= 1
                if k < -1000000: # Safety break
                    break

        # 2. Sell Orders (Above current price)
        if mode in ["neutral", "short"] and sell_window > 0:
            start_k = int((bid - anchor) / step)
            start_k -= 2 # Check a bit lower
            
            count = 0 
            k = start_k
            
            # Iterate upwards
            while count < sell_window:
                if (anchor + k * step) > (max_price + step): # Stop if above max
                    break

                level = self._normalize_price(anchor + k * step)
                
                if level > bid and level <= max_price and level >= min_price:
                    if not _should_skip(level, "sell"):
                        target_sells.append(level)
                        count += 1
                
                k += 1
                if k > 1000000: # Safety break
                    break
            
            target_sells.sort(reverse=True)

        return target_buys, target_sells
