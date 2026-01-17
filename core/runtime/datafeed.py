"""Market data feed and indicator cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Tuple

import MetaTrader5 as mt5
import numpy as np


@dataclass
class _AtrState:
    last_value: float | None = None
    last_time: float = 0.0


class DataFeed:
    def __init__(self, broker):
        self.broker = broker
        self._atr_cache: Dict[Tuple[str, int, int, str, float], _AtrState] = {}

    def get_atr(
        self,
        symbol: str,
        timeframe: int,
        period: int,
        mode: str,
        smooth: float,
        update_seconds: float,
    ) -> float | None:
        key = (symbol, timeframe, int(period), str(mode or "").lower(), float(smooth))
        state = self._atr_cache.get(key)
        if state is None:
            state = _AtrState()
            self._atr_cache[key] = state

        now = time.time()
        if (now - state.last_time) < float(update_seconds):
            return state.last_value

        rates = self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(period) + 1)
        if rates is None or len(rates) < int(period) + 1:
            return state.last_value

        highs = rates["high"]
        lows = rates["low"]
        closes = rates["close"]
        prev_closes = closes[:-1]
        curr_highs = highs[1:]
        curr_lows = lows[1:]

        tr = np.maximum(
            curr_highs - curr_lows,
            np.maximum(abs(curr_highs - prev_closes), abs(curr_lows - prev_closes)),
        )

        mode = str(mode or "wilder").lower()
        if mode == "sma":
            raw_atr = float(np.mean(tr))
        elif mode == "ema":
            if state.last_value is None:
                raw_atr = float(np.mean(tr))
            else:
                alpha = 2.0 / (period + 1.0)
                raw_atr = (state.last_value * (1 - alpha)) + (tr[-1] * alpha)
        else:
            if state.last_value is None:
                raw_atr = float(np.mean(tr))
            else:
                raw_atr = (state.last_value * (period - 1) + tr[-1]) / period

        if smooth:
            if state.last_value is None:
                state.last_value = raw_atr
            else:
                smooth = float(smooth)
                if 0 < smooth < 1:
                    state.last_value = (state.last_value * (1 - smooth)) + (raw_atr * smooth)
                else:
                    state.last_value = raw_atr
        else:
            state.last_value = raw_atr

        state.last_time = now
        return state.last_value
