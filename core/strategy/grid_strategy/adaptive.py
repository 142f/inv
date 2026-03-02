import MetaTrader5 as mt5
import numpy as np
import time
from core.runtime import DataFeed

class GridAdaptiveMixin:
    # 辅助方法：统一时间周期解析，减少重复格式化 (优化：逻辑归并)
    def _get_mapped_tf(self, tf_str: str) -> int:
        return self._TIMEFRAME_MAP.get(str(tf_str or "M15").upper(), mt5.TIMEFRAME_M15)

    def _resolve_timeframe(self):
        return self._get_mapped_tf(self.atr_timeframe)

    def _resolve_adaptive_timeframe(self):
        return self._get_mapped_tf(self.adaptive_timeframe or self.atr_timeframe)

    def _calculate_atr_series(self, tr: np.ndarray, period: int, mode: str) -> np.ndarray:
        # [优化] 预处理参数，避免在循环中重复判断
        p = max(1, period)
        tr_len = len(tr)
        if tr_len < p:
            return np.array([], dtype=float)
        
        m = str(mode or "wilder").lower()
        if m == "sma":
            return np.convolve(tr, np.ones(p), "valid") / p

        # [性能优化] 使用预分配 NumPy 数组替代 list.append，避免 Python 对象拆箱/装箱开销
        alpha = 1.0 / p if m == "wilder" else 2.0 / (p + 1.0)
        out_len = tr_len - p + 1
        atr_series = np.empty(out_len, dtype=float)
        
        current_atr = np.mean(tr[:p])
        atr_series[0] = current_atr
        
        # 将 TR 转换为 list 仅为在 Python 原生循环中提速 (NumPy 索引在标量循环中较慢)
        tr_list = tr.tolist()
        for i in range(p, tr_len):
            current_atr = (current_atr * (1.0 - alpha)) + (tr_list[i] * alpha)
            atr_series[i - p + 1] = current_atr
        return atr_series

    def _apply_atr_targets(self, atr_value: float, *, step_mult: float = 1.0) -> None:
        if atr_value is None or atr_value <= 0 or self.atr_factor <= 0:
            return

        precision = max(1, int(self.digits))
        raw = atr_value * self.atr_factor * step_mult
        
        # [优化] 将常量计算移出闭包外部 (虽闭包已保留，但优化了 base 逻辑)
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
                self.symbol, timeframe, lookback + 2,
                cache_seconds=2.0, min_ratio=0.7,
            )
        else:
            rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, timeframe, 0, lookback + 2)

        if rates is None or len(rates) < 10:
            return

        last_closed_time = int(rates[-2]["time"])
        if last_closed_time == self._last_adapt_bar_time:
            return
        self._last_adapt_bar_time = last_closed_time

        # [优化] 使用统一的 TR 计算逻辑，减少重复的 slice 操作
        rates_comp = rates[:-1]
        tr = self._get_tr_vectorized(rates_comp)
        
        atr_series = self._calculate_atr_series(tr, self.atr_period, self.atr_mode)
        if atr_series.size == 0:
            return

        atr_current = float(atr_series[-1])
        # [性能优化] 统计分位数计算聚合，减少对同一数组的多次遍历
        q_low, q_high, atr_median = np.quantile(atr_series, [self.adaptive_quantile_low, self.adaptive_quantile_high, 0.5])

        # Regime 判断逻辑保留（含潜在逻辑漏洞，如 step_mult 未定义覆盖等）
        step_mult = 1.0
        if atr_current <= q_low:
            step_mult = self.adaptive_step_mult_low
        elif atr_current >= q_high:
            step_mult = self.adaptive_step_mult_high

        self._apply_atr_targets(atr_current, step_mult=step_mult)

        if atr_current > 0 and self.base_lot > 0:
            lot_mult = max(self.adaptive_lot_min_mult, 
                           min(self.adaptive_lot_max_mult, atr_median / atr_current))
            self.lot = max(0.0, self.base_lot * lot_mult)

        buffer = atr_current * self.adaptive_range_buffer_atr
        computed_min = float(np.min(rates_comp["low"])) - buffer
        computed_max = float(np.max(rates_comp["high"])) + buffer
        # [修复 L-06] 与用户设定的硬边界取交集，防止 adaptive 把网格扩张到危险价位。
        # 若用户未设置有效边界（默认 0 / 999999），clamp 不产生实质约束。
        user_min = getattr(self, '_user_min_price', 0.0)
        user_max = getattr(self, '_user_max_price', float('inf'))
        self.min_price = max(computed_min, user_min)
        self.max_price = min(computed_max, user_max)

    # [优化：新增私有辅助] 提取矢量化 TR 计算，消除代码重复
    @staticmethod
    def _get_tr_vectorized(rates: np.ndarray) -> np.ndarray:
        h = rates["high"][1:]
        l = rates["low"][1:]
        pc = rates["close"][:-1]
        return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))

    def _calculate_atr(self):
        current_time = time.time()
        if current_time - self._last_atr_time < self.atr_update_seconds:
            return self._last_atr_value

        lookback = max(self.atr_period * 5, 100)
        rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, self._resolve_timeframe(), 0, lookback + 1)

        if rates is None or len(rates) < self.atr_period + 2:
            return self._last_atr_value

        # [优化] 调用统一的 TR 计算，移除冗余的 np.abs 重复调用
        tr = self._get_tr_vectorized(rates[:-1])

        if len(tr) < self.atr_period:
            return self._last_atr_value

        raw_atr = float(DataFeed._calculate_raw_atr(tr, self.atr_period, str(self.atr_mode or "wilder").lower()))
        self._last_atr_value = DataFeed._smooth_atr(raw_atr, self._last_atr_value, self.atr_smooth)
        self._last_atr_time = current_time
        return self._last_atr_value