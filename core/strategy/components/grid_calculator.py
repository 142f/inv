"""Grid target calculator."""

from __future__ import annotations

from typing import Callable, List, Optional, Set, Tuple


class GridCalculator:
    # Safety guard for unexpected parameter combinations.
    _MAX_ITERATIONS = 10000

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
        # Keep API compatibility: recenter_steps is intentionally unused here.
        _ = recenter_steps

        target_buys: List[float] = []
        target_sells: List[float] = []

        if step <= 0:
            return target_buys, target_sells
        if max_price <= min_price:
            return target_buys, target_sells
        if bid <= 0 or ask <= 0 or ask < bid:
            return target_buys, target_sells

        blocked_levels = blocked_k or set()
        safe_min_dist = max(0.0, float(min_dist)) if min_dist is not None else 0.0

        if mode in ("neutral", "long") and buy_window > 0:
            start_k = int((ask - anchor) / step) + 3
            target_buys = self._collect_side_targets(
                side="buy",
                start_k=start_k,
                k_delta=-1,
                window=buy_window,
                anchor=anchor,
                step=step,
                min_price=min_price,
                max_price=max_price,
                bid=bid,
                ask=ask,
                min_dist=safe_min_dist,
                blocked_k=blocked_levels,
            )

        if mode in ("neutral", "short") and sell_window > 0:
            start_k = int((bid - anchor) / step) - 3
            target_sells = self._collect_side_targets(
                side="sell",
                start_k=start_k,
                k_delta=1,
                window=sell_window,
                anchor=anchor,
                step=step,
                min_price=min_price,
                max_price=max_price,
                bid=bid,
                ask=ask,
                min_dist=safe_min_dist,
                blocked_k=blocked_levels,
            )
            target_sells.sort(reverse=True)

        return target_buys, target_sells

    def _collect_side_targets(
        self,
        *,
        side: str,
        start_k: int,
        k_delta: int,
        window: int,
        anchor: float,
        step: float,
        min_price: float,
        max_price: float,
        bid: float,
        ask: float,
        min_dist: float,
        blocked_k: Set[int],
    ) -> List[float]:
        targets: List[float] = []
        k = start_k
        iterations = 0

        while len(targets) < window and iterations < self._MAX_ITERATIONS:
            iterations += 1
            level = self._normalize_price(anchor + k * step)

            if side == "buy":
                if level < min_price - step:
                    break
                is_candidate = level < ask and min_price <= level <= max_price
            else:
                if level > max_price + step:
                    break
                is_candidate = level > bid and min_price <= level <= max_price

            if is_candidate and not self._should_skip_level(
                side=side,
                level=level,
                anchor=anchor,
                step=step,
                bid=bid,
                ask=ask,
                min_dist=min_dist,
                blocked_k=blocked_k,
            ):
                targets.append(level)

            k += k_delta

        return targets

    @staticmethod
    def _should_skip_level(
        *,
        side: str,
        level: float,
        anchor: float,
        step: float,
        bid: float,
        ask: float,
        min_dist: float,
        blocked_k: Set[int],
    ) -> bool:
        if min_dist > 0.0:
            if side == "buy" and (ask - level) < min_dist:
                return True
            if side == "sell" and (level - bid) < min_dist:
                return True

        if blocked_k:
            level_k = round((level - anchor) / step)
            if level_k in blocked_k:
                return True

        return False
