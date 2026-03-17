from __future__ import annotations


class InventoryWindowPolicy:
    """Dynamic windows driven by inventory pressure."""

    def compute(
        self,
        *,
        base_buy: int,
        base_sell: int,
        mode: str,
        max_net_vol: float | None,
        predicted_net_vol: float,
    ) -> tuple[int, int, float]:
        buy = max(0, int(base_buy))
        sell = max(0, int(base_sell))
        cap = float(max_net_vol or 0.0)
        if cap <= 0:
            return buy, sell, 0.0

        pressure = max(-1.5, min(1.5, float(predicted_net_vol) / cap))
        if pressure >= 0:
            buy_scale = max(0.2, 1.0 - 0.8 * pressure)
            sell_scale = min(2.5, 1.0 + 0.6 * pressure)
        else:
            p = abs(pressure)
            buy_scale = min(2.5, 1.0 + 0.6 * p)
            sell_scale = max(0.2, 1.0 - 0.8 * p)

        buy = max(0, int(round(buy * buy_scale)))
        sell = max(0, int(round(sell * sell_scale)))

        mode_norm = str(mode or "neutral").lower().strip()
        if mode_norm == "long":
            sell = 0
        elif mode_norm == "short":
            buy = 0
        return buy, sell, pressure

