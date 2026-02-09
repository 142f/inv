# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import time
from core.logger import Logger

class GridSymbolMixin:
    def set_symbol(self, new_symbol: str, *, reset_runtime_state: bool = True):
        """切换交易品种，并刷新品种信息缓存。

        为什么需要它：digits/point/stop_level/最小下单量等都是按 symbol 缓存的，
        直接改 self.symbol 会导致后续归一化/风控使用旧品种参数。

        :param reset_runtime_state: True 时会清空 anchor、ATR 缓存、对冲运行态等，避免跨品种串状态。
        """
        if not new_symbol or new_symbol == self.symbol:
            return

        self.symbol = new_symbol
        # 刷新品种静态信息缓存
        self._cache_symbol_info()

        if reset_runtime_state:
            self.anchor = None
            self._last_recenter_time = 0.0
            self.pause_until = 0.0

            # ATR 缓存
            self._last_atr_value = None
            self._last_atr_time = 0.0
            self._last_adapt_bar_time = 0.0

            # 对冲运行态
            self._last_hedge_time = 0.0
            self._last_hedge_entry_price = None

            # 行情缓存

    @staticmethod
    def _precision_from_step(step: float) -> int:
        if step <= 0:
            return 2
        text = f"{step:.10f}".rstrip("0").rstrip(".")
        if "." in text:
            return len(text.split(".")[1])
        return 0

    def _cache_symbol_info(self):
        info = self._mt5_call(mt5.symbol_info, self.symbol)
            
        if info:
            self.digits = info.digits
            self.point = info.point
            self.stop_level = info.trade_stops_level * info.point
            self.vol_min = info.volume_min
            self.vol_max = info.volume_max
            self.vol_step = info.volume_step
            self.vol_precision = self._precision_from_step(self.vol_step)
            self.filling_mode = getattr(info, "filling_mode", None)
            self.initialized = True
        else:
            self.digits = 2
            self.point = 0.01
            self.stop_level = 0
            self.vol_min = 0.01
            self.vol_max = 100
            self.vol_step = 0.01
            self.vol_precision = 2
            self.filling_mode = None
            self.initialized = False
            Logger.log(self.symbol, "WARN", "初始化获取品种信息失败，使用默认值")

    def _normalize_price(self, price):
        return float(round(price, self.digits))

    def _normalize_volume(self, vol):
        # 简单的步长取整
        if self.vol_step > 0:
            steps = round(vol / self.vol_step)
            vol = steps * self.vol_step
        precision = getattr(self, "vol_precision", 2)
        return float(round(max(self.vol_min, min(self.vol_max, vol)), precision))

    def _get_grid_level(self, price, anchor):
        """以 anchor 为锚点，把 price snap 到最近的网格线"""
        if self.step <= 0: return price
        k = round((price - anchor) / self.step)
        return anchor + k * self.step

    def _init_anchor_if_needed(self, mid_price):
        if self.anchor is None:
            if self.step <= 0:
                self.anchor = self._normalize_price(mid_price)
                return
            # 用当前价格作为初始anchor，并snap到网格线上
            base0 = round(mid_price / self.step) * self.step
            self.anchor = self._normalize_price(base0)
            Logger.log(self.symbol, "INIT", f"Anchor Initialized | Price={self.anchor:.{self.digits}f}")

    def _maybe_recenter(self, mid_price):
        """触发条件：偏离>=recenter_steps*step 且超过冷却时间"""
        if self.step <= 0 or self.anchor is None:
            return False
        now = time.time()
        if now - self._last_recenter_time < self.recenter_cooldown:
            return False

        drift_steps = (mid_price - self.anchor) / self.step
        if abs(drift_steps) < self.recenter_steps:
            return False

        # 平移anchor到“当前价格所在网格线”
        new_anchor = self._get_grid_level(mid_price, self.anchor)
        self.anchor = self._normalize_price(new_anchor)
        self._last_recenter_time = now
        Logger.log(self.symbol, "RECENTER", f"Anchor Shifted | New={self.anchor:.{self.digits}f} MidPrice={mid_price:.{self.digits}f}")
        return True
