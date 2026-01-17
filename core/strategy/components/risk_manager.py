"""Risk checks and guards."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpreadCheck:
    triggered: bool
    spread: float
    pause_until: float


class RiskManager:
    def check_spread(
        self,
        *,
        bid: float,
        ask: float,
        max_spread_points: float | None,
        point: float,
        extreme_cooldown: float,
        now: float,
    ) -> SpreadCheck:
        if max_spread_points is None:
            return SpreadCheck(False, 0.0, 0.0)

        spread = ask - bid
        if spread > max_spread_points * point:
            return SpreadCheck(True, spread, now + extreme_cooldown)
        return SpreadCheck(False, spread, 0.0)

    def check_range(
        self,
        *,
        mid_price: float,
        min_price: float,
        max_price: float,
        out_of_range_action: str,
    ) -> str:
        if mid_price < min_price or mid_price > max_price:
            return out_of_range_action
        return "ok"
