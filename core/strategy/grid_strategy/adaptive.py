# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import numpy as np
import time
from core.runtime import DataFeed

class GridAdaptiveMixin:
    def _resolve_timeframe(self):
        timeframe = str(self.atr_timeframe or "M15").upper()
        return self._TIMEFRAME_MAP.get(timeframe, mt5.TIMEFRAME_M15)

    def _resolve_adaptive_timeframe(self):
        timeframe = str(self.adaptive_timeframe or self.atr_timeframe or "M15").upper()
        return self._TIMEFRAME_MAP.get(timeframe, mt5.TIMEFRAME_M15)

    def _calculate_atr_series(self, tr, period: int, mode: str) -> np.ndarray:
        period = max(1, int(period))
        if len(tr) < period:
            return np.array([], dtype=float)
        mode = str(mode or "wilder").lower()
        if mode == "sma":
            atr = np.convolve(tr, np.ones(period), "valid") / period
            return atr

        alpha = 1.0 / period if mode == "wilder" else 2.0 / (period + 1.0)
        atr_values = []
        current_atr = float(np.mean(tr[:period]))
        atr_values.append(current_atr)
        for i in range(period, len(tr)):
            current_atr = (current_atr * (1.0 - alpha)) + (tr[i] * alpha)
            atr_values.append(current_atr)
        return np.array(atr_values, dtype=float)

    def _apply_atr_targets(self, atr_value: float, *, step_mult: float = 1.0) -> None:
        if atr_value is None or atr_value <= 0 or self.atr_factor <= 0:
            return

        precision = max(1, int(self.digits))
        raw = atr_value * self.atr_factor * step_mult

        def _clamp_and_apply(base_val: float, current: float) -> float:
            base = max(float(base_val), self.point)
            lo = max(base * self.min_step_mult, self.point)
            hi = max(base * self.max_step_mult, lo)
            clamped = round(max(lo, min(hi, raw)), precision)
            if abs(clamped - current) / max(current, 1e-9) > self.atr_change_threshold:
                return clamped
            return current

        self.step = _clamp_and_apply(self.base_step, self.step)
        self.tp_dist = _clamp_and_apply(self.base_tp_dist, self.tp_dist)

    def _maybe_adapt_params(self):
        if not self.adaptive_enabled or not self.use_atr:
            return

        lookback = max(50, int(self.adaptive_lookback))
        timeframe = self._resolve_adaptive_timeframe()

        if self.datafeed is not None:
            rates = self.datafeed.get_rates(
                self.symbol,
                timeframe,
                lookback + 2,
                cache_seconds=2.0,
                min_ratio=0.7,
            )
        else:
            rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, timeframe, 0, lookback + 2)

        if rates is None or len(rates) < 10:
            return

        last_closed_time = int(rates[-2]["time"])
        if last_closed_time == self._last_adapt_bar_time:
            return
        self._last_adapt_bar_time = last_closed_time

        # Drop the last incomplete bar.
        rates = rates[:-1]
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        close_prev = rates["close"][:-1]
        tr = np.maximum(highs - lows, np.maximum(abs(highs - close_prev), abs(lows - close_prev)))
        atr_series = self._calculate_atr_series(tr, self.atr_period, self.atr_mode)
        if atr_series.size == 0:
            return

        atr_current = float(atr_series[-1])
        q_low = float(np.quantile(atr_series, self.adaptive_quantile_low))
        q_high = float(np.quantile(atr_series, self.adaptive_quantile_high))

        # Step/tp scaling by volatility regime.
        step_mult = 1.0
        if atr_current <= q_low:
            step_mult = self.adaptive_step_mult_low
        elif atr_current >= q_high:
            step_mult = self.adaptive_step_mult_high

        self._apply_atr_targets(atr_current, step_mult=step_mult)

        # Lot scaling inversely to ATR, with caps.
        atr_median = float(np.median(atr_series))
        if atr_current > 0 and self.base_lot > 0:
            lot_mult = atr_median / atr_current
            lot_mult = max(self.adaptive_lot_min_mult, min(self.adaptive_lot_max_mult, lot_mult))
            self.lot = max(0.0, self.base_lot * lot_mult)

        # Range update based on recent high/low + ATR buffer.
        buffer = atr_current * self.adaptive_range_buffer_atr
        self.min_price = float(np.min(rates["low"])) - buffer
        self.max_price = float(np.max(rates["high"])) + buffer

    def _calculate_atr(self):
        """Standard ATR calculation based on history to avoid intra-bar recursion errors."""
        current_time = time.time()
        if current_time - self._last_atr_time < self.atr_update_seconds:
            return self._last_atr_value

        # Determine lookback length
        # We need enough history for EMA/Wilder to converge from a simple SMA seed.
        # 5x period is usually sufficient (weight of seed < 1%).
        lookback = max(self.atr_period * 5, 100)

        rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, self._resolve_timeframe(), 0, lookback + 1)

        # Check data sufficiency
        if rates is None or len(rates) < self.atr_period + 2:
            return self._last_atr_value

        # Calculate True Range (TR) using completed bars only.
        # rates are in chronological order; drop the last (incomplete) bar.
        rates = rates[:-1]
        high = rates["high"][1:]
        low = rates["low"][1:]
        close_prev = rates["close"][:-1]

        tr = np.maximum(high - low, np.abs(high - close_prev))
        tr = np.maximum(tr, np.abs(low - close_prev))

        # We need at least 'period' data points
        if len(tr) < self.atr_period:
            return self._last_atr_value

        mode = str(self.atr_mode or "wilder").lower()
        raw_atr = float(DataFeed._calculate_raw_atr(tr, self.atr_period, mode))

        # Apply optional output smoothing (Low-pass filter on the output only)
        self._last_atr_value = DataFeed._smooth_atr(raw_atr, self._last_atr_value, self.atr_smooth)

        self._last_atr_time = current_time
        return self._last_atr_value
