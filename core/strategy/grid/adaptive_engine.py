import time
import numpy as np
import MetaTrader5 as mt5
from typing import Optional, Dict

from .params import AdaptiveParams, AtrParams
from .state import RuntimeState
from .mt5_gateway import MT5Gateway
from core.runtime.datafeed import DataFeed
from .params import GridParams

class AdaptiveEngine:
    _TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }

    def __init__(self, 
                 adaptive_params: AdaptiveParams, 
                 atr_params: AtrParams,
                 grid_params: GridParams,
                 gateway: MT5Gateway, 
                 state: RuntimeState,
                 symbol: str, 
                 digits: int,
                 point: float):
        
        self.adaptive_params = adaptive_params
        self.atr_params = atr_params
        self.grid_params = grid_params
        self.gateway = gateway
        self.state = state
        self.symbol = symbol
        self.digits = digits
        self.point = point

        # Base values for restoration
        self.base_step = float(grid_params.step)
        self.base_tp_dist = float(grid_params.tp_dist)
        self.base_lot = float(grid_params.lot)

    def _resolve_timeframe(self, tf_str: str):
        timeframe = str(tf_str or "M15").upper()
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

    def _apply_atr_targets(self, atr_value: float, step_mult: float = 1.0) -> None:
        if atr_value is None or atr_value <= 0 or self.atr_params.atr_factor <= 0:
            return

        precision = max(1, int(self.digits))
        raw = atr_value * self.atr_params.atr_factor * step_mult

        def _clamp_and_apply(base_val: float, current: float) -> float:
            base = max(float(base_val), self.point)
            lo = max(base * self.atr_params.min_step_mult, self.point)
            hi = max(base * self.atr_params.max_step_mult, lo)
            clamped = round(max(lo, min(hi, raw)), precision)
            if abs(clamped - current) / max(current, 1e-9) > self.atr_params.atr_change_threshold:
                return clamped
            return current

        # Update Grid Params directly? Or return new values?
        # Ideally we update the object that holds current execution params. 
        # Since GridParams is a dataclass, we can modify it if the strategy holds a specific instance for 'current' params.
        # However, grid_params passed here might be the config source.
        # We will modify self.grid_params assuming it's the active instance.
        
        self.grid_params.step = _clamp_and_apply(self.base_step, self.grid_params.step)
        self.grid_params.tp_dist = _clamp_and_apply(self.base_tp_dist, self.grid_params.tp_dist)

    def maybe_adapt_params(self, datafeed: Optional[DataFeed]):
        if not self.adaptive_params.enabled or not self.atr_params.use_atr:
            return

        lookback = max(50, int(self.adaptive_params.lookback))
        timeframe = self._resolve_timeframe(self.adaptive_params.timeframe)

        if datafeed is not None:
            rates = datafeed.get_rates(
                self.symbol,
                timeframe,
                lookback + 2,
                cache_seconds=2.0,
                min_ratio=0.7,
            )
        else:
            rates = self.gateway.copy_rates_from_pos(self.symbol, timeframe, 0, lookback + 2)

        if rates is None or len(rates) < 10:
            return

        # Check if we have a new closed bar
        last_closed_time = int(rates[-2]["time"])
        # Use state to track last adapt time
        # We might need to add `last_adapt_bar_time` to RuntimeState if it's not there.
        # It's not in RuntimeState currently. We can add it or store it locally if it doesn't need persistence across restarts.
        # Strategy_lib persisted it. Let's use a local var or valid attribute.
        if not hasattr(self.state, 'last_adapt_bar_time'):
            self.state.last_adapt_bar_time = 0
            
        if last_closed_time == self.state.last_adapt_bar_time:
            return
        self.state.last_adapt_bar_time = last_closed_time

        rates = rates[:-1]
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        close_prev = rates["close"][:-1]
        tr = np.maximum(highs - lows, np.maximum(abs(highs - close_prev), abs(lows - close_prev)))
        atr_series = self._calculate_atr_series(tr, self.atr_params.atr_period, self.atr_params.atr_mode)
        if atr_series.size == 0:
            return

        atr_current = float(atr_series[-1])
        q_low = float(np.quantile(atr_series, self.adaptive_params.quantile_low))
        q_high = float(np.quantile(atr_series, self.adaptive_params.quantile_high))

        step_mult = 1.0
        if atr_current <= q_low:
            step_mult = self.adaptive_params.step_mult_low
        elif atr_current >= q_high:
            step_mult = self.adaptive_params.step_mult_high

        self._apply_atr_targets(atr_current, step_mult=step_mult)

        atr_median = float(np.median(atr_series))
        if atr_current > 0 and self.base_lot > 0:
            lot_mult = atr_median / atr_current
            lot_mult = max(self.adaptive_params.lot_min_mult, min(self.adaptive_params.lot_max_mult, lot_mult))
            self.grid_params.lot = max(0.0, self.base_lot * lot_mult)

        buffer = atr_current * self.adaptive_params.range_buffer_atr
        self.grid_params.min_price = float(np.min(rates["low"])) - buffer
        self.grid_params.max_price = float(np.max(rates["high"])) + buffer

    def calculate_atr(self):
        """Standard ATR calculation based on history."""
        current_time = time.time()
        if current_time - self.state.last_atr_time < self.atr_params.atr_update_seconds:
            return self.state.last_atr_value

        lookback = max(self.atr_params.atr_period * 5, 100)
        timeframe = self._resolve_timeframe(self.atr_params.atr_timeframe)
        
        rates = self.gateway.copy_rates_from_pos(self.symbol, timeframe, 0, lookback + 1)

        if rates is None or len(rates) < self.atr_params.atr_period + 2:
            return self.state.last_atr_value

        rates = rates[:-1]
        high = rates["high"][1:]
        low = rates["low"][1:]
        close_prev = rates["close"][:-1]

        tr = np.maximum(high - low, np.abs(high - close_prev))
        tr = np.maximum(tr, np.abs(low - close_prev))

        if len(tr) < self.atr_params.atr_period:
            return self.state.last_atr_value

        mode = str(self.atr_params.atr_mode or "wilder").lower()
        raw_atr = float(DataFeed._calculate_raw_atr(tr, self.atr_params.atr_period, mode))

        self.state.last_atr_value = DataFeed._smooth_atr(raw_atr, self.state.last_atr_value, self.atr_params.atr_smooth)
        self.state.last_atr_time = current_time
        return self.state.last_atr_value
