"""Market data feed and indicator cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple

import MetaTrader5 as mt5
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
        key = (symbol, timeframe, int(period), str(mode or "").lower(), float(smooth))
        state = self._atr_cache.get(key)
        if state is None:
            state = _AtrState()
            self._atr_cache[key] = state

        now = time.time()
        if (now - state.last_time) < float(update_seconds):
            return state.last_value

        # 增加数据量以确保 EMA/Wilder 计算能够收敛 (5x period 通常足够)
        lookback = max(int(period) * 5, 50)
        if getattr(self.broker, "lock", None) is not None:
            with self.broker.lock:
                rates = self.broker.copy_rates_from_pos(symbol, timeframe, 0, lookback + 1)
        else:
            rates = self.broker.copy_rates_from_pos(symbol, timeframe, 0, lookback + 1)
        if rates is None or len(rates) < int(period) + 1:
            return state.last_value

        # 丢弃最后一根未完成的 K 线
        rates = rates[:-1]
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        prev_closes = rates["close"][:-1]

        tr = np.maximum(
            highs - lows,
            np.maximum(abs(highs - prev_closes), abs(lows - prev_closes)),
        )

        if len(tr) < int(period):
            return state.last_value

        mode = str(mode or "wilder").lower()
        if mode == "sma":
            raw_atr = float(np.mean(tr[-int(period):]))
        else:
            # Wilder (alpha=1/N) 或 EMA (alpha=2/(N+1))
            # 使用 SMA 初始化，然后递归计算
            current_atr = float(np.mean(tr[:int(period)]))
            alpha = 2.0 / (period + 1.0) if mode == "ema" else 1.0 / period
            for i in range(int(period), len(tr)):
                current_atr = (current_atr * (1.0 - alpha)) + (tr[i] * alpha)
            raw_atr = current_atr

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

    def get_rates(
        self,
        symbol: str,
        timeframe: int,
        count: int,
        *,
        cache_seconds: float = 1.0,
        min_ratio: float = 0.7,
    ) -> Any:
        key = (symbol, timeframe, int(count))
        state = self._rates_cache.get(key)
        if state is None:
            state = _RatesState()
            self._rates_cache[key] = state

        now = time.time()
        if state.rates is not None and (now - state.last_time) < float(cache_seconds):
            return state.rates

        if getattr(self.broker, "lock", None) is not None:
            with self.broker.lock:
                rates = self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(count))
        else:
            rates = self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(count))

        if rates is None or len(rates) < int(count * float(min_ratio)):
            return state.rates

        state.rates = rates
        state.last_time = now
        return rates
