"""Market data feed and indicator cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple

import numpy as np


@dataclass
class _AtrState:
    last_value: float | None = None
    last_time: float = 0.0


@dataclass
class _RatesState:
    last_time: float = 0.0
    rates: Any | None = None


class DataFeed:
    def __init__(self, broker):
        self.broker = broker
        self._atr_cache: Dict[Tuple[str, int, int, str, float], _AtrState] = {}
        self._rates_cache: Dict[Tuple[str, int, int], _RatesState] = {}

    def get_atr(
        self,
        symbol: str,
        timeframe: int,
        period: int,
        mode: str,
        smooth: float,
        update_seconds: float,
    ) -> float | None:
        period = int(period)
        smooth = float(smooth)
        normalized_mode = str(mode or "").lower()

        key = (symbol, timeframe, period, normalized_mode, smooth)
        state = self._atr_cache.setdefault(key, _AtrState())

        now = self._now()
        update_seconds = float(update_seconds)
        if (now - state.last_time) < update_seconds:
            return state.last_value

        # Fetch enough bars so EMA/Wilder recursion converges from SMA seed.
        lookback = max(period * 5, 50)
        rates = self._copy_rates(symbol, timeframe, lookback + 1)
        if rates is None or len(rates) < period + 1:
            return state.last_value

        rates = rates[:-1]  # Ignore the currently forming bar.
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        prev_closes = rates["close"][:-1]

        tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)))
        if len(tr) < period:
            return state.last_value

        raw_atr = self._calculate_raw_atr(tr, period, normalized_mode)
        state.last_value = self._smooth_atr(raw_atr, state.last_value, smooth)
        state.last_time = now
        return state.last_value

    def get_rates(
        self,
        symbol: str,
        timeframe: int,
        count: int,
        *,
        cache_seconds: float = 1.0,
        min_ratio: float = 0.7,
    ) -> Any:
        count = int(count)
        key = (symbol, timeframe, count)
        state = self._rates_cache.setdefault(key, _RatesState())

        now = self._now()
        cache_seconds = float(cache_seconds)
        if state.rates is not None and (now - state.last_time) < cache_seconds:
            return state.rates

        rates = self._copy_rates(symbol, timeframe, count)
        min_count = int(count * float(min_ratio))
        if rates is None or len(rates) < min_count:
            return state.rates

        state.rates = rates
        state.last_time = now
        return rates

    def _copy_rates(self, symbol: str, timeframe: int, count: int):
        if getattr(self.broker, "lock", None) is not None:
            with self.broker.lock:
                return self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(count))
        return self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(count))

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    @staticmethod
    def _calculate_raw_atr(tr: np.ndarray, period: int, mode: str) -> float:
        if mode == "sma":
            return float(np.mean(tr[-period:]))

        current_atr = float(np.mean(tr[:period]))
        alpha = 2.0 / (period + 1.0) if mode == "ema" else 1.0 / period
        for i in range(period, len(tr)):
            current_atr = (current_atr * (1.0 - alpha)) + (tr[i] * alpha)
        return float(current_atr)

    @staticmethod
    def _smooth_atr(raw_atr: float, last_value: float | None, smooth: float) -> float:
        if not smooth or last_value is None:
            return raw_atr
        if 0 < smooth < 1:
            return (last_value * (1 - smooth)) + (raw_atr * smooth)
        return raw_atr
