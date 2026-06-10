from __future__ import annotations

from typing import Callable, Iterable, List


class UtilityOrderSelector:
    """Rank candidate targets by utility (reward - cost - inventory risk)."""

    def __init__(
        self,
        *,
        cost_weight: float = 0.35,
        distance_weight: float = 0.2,
        risk_weight: float = 0.7,
    ) -> None:
        self.cost_weight = float(cost_weight)
        self.distance_weight = float(distance_weight)
        self.risk_weight = float(risk_weight)

    def rank(
        self,
        *,
        side: str,
        targets: Iterable[float],
        tick_bid: float,
        tick_ask: float,
        step: float,
        tp_dist: float,
        point: float,
        lot: float,
        max_net_vol: float | None,
        predicted_net_vol: float,
        fill_probability: Callable[[str, float], float],
    ) -> List[float]:
        targets = list(targets)
        if not targets:
            return targets

        cap = float(max_net_vol or 0.0)
        spread = max(0.0, float(tick_ask - tick_bid))
        ranked = []
        side_norm = str(side).lower().strip()

        for price in targets:
            p_fill = float(fill_probability(side_norm, float(price)))
            projected = float(predicted_net_vol) + (lot * p_fill if side_norm == "buy" else -lot * p_fill)
            directional_pressure = 0.0
            if cap > 0:
                directional_pressure = projected / cap
                if side_norm == "sell":
                    directional_pressure *= -1.0
                directional_pressure = max(0.0, min(2.0, directional_pressure))

            reward = float(tp_dist) * p_fill
            distance = (tick_ask - price) if side_norm == "buy" else (price - tick_bid)
            distance_penalty = max(0.0, float(distance) - float(step) * 0.5)
            cost_penalty = spread + self.distance_weight * distance_penalty
            risk_penalty = float(step) * self.risk_weight * directional_pressure
            utility = reward - self.cost_weight * cost_penalty - risk_penalty
            ranked.append((utility, float(price)))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [price for _, price in ranked]
