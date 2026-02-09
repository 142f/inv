# strategy_lib.py
import MetaTrader5 as mt5
import time
import numpy as np
from .logger import Logger
from core.strategy.components import GridCalculator, RiskManager
from core.strategy.components.risk_manager import RangeAction
from core.runtime.datafeed import DataFeed

class _QueuedResult:
    def __init__(self):
        self.retcode = mt5.TRADE_RETCODE_DONE
        self.comment = "QUEUED"
        self.order = 0

class GridStrategy:
    _TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    _VALID_MODES = {"neutral", "long", "short"}

    def __init__(self, symbol, step, tp_dist, lot, magic, 
                 sl_dist=0, window=6, min_p=0, max_p=999999, enabled=True, 
                 use_atr=False, atr_period=14, atr_factor=1.0, atr_mode="wilder", atr_timeframe="M15",
                 adaptive_enabled=False, adaptive_timeframe="M15", adaptive_lookback=200,
                 adaptive_quantile_low=0.30, adaptive_quantile_high=0.70,
                 adaptive_step_mult_low=0.90, adaptive_step_mult_high=1.10,
                 adaptive_lot_min_mult=0.50, adaptive_lot_max_mult=1.50,
                 adaptive_range_buffer_atr=1.0,
                 mode="neutral", buy_window=None, sell_window=None, 
                 out_of_range_action="freeze", 
                 atr_update_seconds=5, atr_smooth=0.1, atr_change_threshold=0.01,
                 min_step_mult=0.5, max_step_mult=3.0,
                 auto_trim=False,
                 lock=None,
                 datafeed=None,
                 # --- Anchor / Recenter ---
                 anchor=None,                 # 初始anchor；None=启动时自动取
                 recenter_steps=3,            # 偏离多少个step触发再中心化
                 recenter_cooldown=30,        # 冷却秒数，避免频繁平移
                 # --- Inventory caps ---
                 max_long_pos=None, max_short_pos=None,
                 max_long_vol=None, max_short_vol=None,
                 max_net_vol=None,            # neutral下建议一定要配
                 # --- Extreme guard ---
                 max_spread_points=None,      # 例如 30 表示 30 points
                 extreme_mode="freeze",       # "freeze" | "reduce_only"
                 extreme_cooldown=30,         # 极端行情触发后的冷却时间(秒)
                 # --- Throttle ---
                 max_new_orders_per_update=10, # 每轮最多补几张单，防止风暴
                 
                 # --- Hedge params ---
                 hedge_enabled=False,
                 hedge_fraction=0.3333,
                 hedge_tranches=3,
                 hedge_entry_steps=1,
                 hedge_exit_steps=1,
                 hedge_cooldown=20,
                 max_gross_vol=None,

                 # --- Gates (volatility / volume) ---
                 hedge_vol_lookback=300,
                 hedge_vol_window=20,
                 hedge_vol_quantile=0.90,
                 hedge_vol_base=200,
                 hedge_vol_mult=3.0,

                 # --- Break-even stop ---
                 be_trigger_steps=1,
                 be_buffer_points=20):
        """
        :param use_atr: 是否启用 ATR 自适应步长
        :param atr_period: ATR 计算周期 (默认 14)
        :param atr_factor: ATR 乘数 (Step = ATR * factor)
        :param atr_mode: ATR 模式: "wilder" | "ema" | "sma"
        :param atr_timeframe: ATR 时间周期 (例如 "M15", "H1")
        :param mode: "neutral" | "long" | "short"
        :param buy_window: 买单窗口大小 (默认等于 window)
        :param sell_window: 卖单窗口大小 (默认等于 window)
        :param out_of_range_action: "freeze" | "stop"
        """
        self.symbol = symbol
        self.base_step = float(step) # 保存初始步长
        self.step = float(step)
        self.base_tp_dist = float(tp_dist)
        self.tp_dist = float(tp_dist)
        self.sl_dist = float(sl_dist)
        self.base_lot = float(lot)
        self.lot = float(lot)
        self.magic = int(magic)
        self.window = int(window)
        self.min_price = float(min_p)
        self.max_price = float(max_p)
        self.enabled = enabled
        self.pause_until = 0
        self.use_atr = use_atr
        self.atr_period = atr_period
        self.atr_factor = atr_factor
        self.atr_mode = atr_mode
        self.atr_timeframe = atr_timeframe
        self.adaptive_enabled = adaptive_enabled
        self.adaptive_timeframe = adaptive_timeframe
        self.adaptive_lookback = int(adaptive_lookback)
        self.adaptive_quantile_low = float(adaptive_quantile_low)
        self.adaptive_quantile_high = float(adaptive_quantile_high)
        self.adaptive_step_mult_low = float(adaptive_step_mult_low)
        self.adaptive_step_mult_high = float(adaptive_step_mult_high)
        self.adaptive_lot_min_mult = float(adaptive_lot_min_mult)
        self.adaptive_lot_max_mult = float(adaptive_lot_max_mult)
        self.adaptive_range_buffer_atr = float(adaptive_range_buffer_atr)
        
        # 新增参数
        self.mode = self._normalize_mode(mode)
        self.buy_window = buy_window if buy_window is not None else window
        self.sell_window = sell_window if sell_window is not None else window
        self.out_of_range_action = out_of_range_action
        
        # ATR 优化参数
        self.atr_update_seconds = atr_update_seconds
        self.atr_smooth = atr_smooth
        self.atr_change_threshold = atr_change_threshold
        self.min_step_mult = min_step_mult
        self.max_step_mult = max_step_mult
        self.auto_trim = bool(auto_trim)
        
        self.lock = lock
        self.datafeed = datafeed
        self.bid_orders = {}
        self.ask_orders = {}
        self._action_collector = None
        self.grid_calculator = GridCalculator(self._normalize_price)
        self.risk_manager = RiskManager()

        # --- Anchor / Risk Control ---
        self.anchor = float(anchor) if anchor is not None else None
        self.recenter_steps = int(recenter_steps)
        self.recenter_cooldown = float(recenter_cooldown)
        self._last_recenter_time = 0

        self.max_long_pos = int(max_long_pos) if max_long_pos is not None else None
        self.max_short_pos = int(max_short_pos) if max_short_pos is not None else None
        self.max_long_vol = float(max_long_vol) if max_long_vol is not None else None
        self.max_short_vol = float(max_short_vol) if max_short_vol is not None else None
        self.max_net_vol = float(max_net_vol) if max_net_vol is not None else None

        self.max_spread_points = float(max_spread_points) if max_spread_points is not None else None
        self.extreme_mode = extreme_mode
        self.extreme_cooldown = float(extreme_cooldown)
        self.max_new_orders_per_update = int(max_new_orders_per_update)
        
        # --- Hedge params ---
        self.hedge_enabled = bool(hedge_enabled)
        self.hedge_fraction = float(hedge_fraction)
        self.hedge_tranches = int(hedge_tranches)
        self.hedge_entry_steps = int(hedge_entry_steps)
        self.hedge_exit_steps = int(hedge_exit_steps)
        self.hedge_cooldown = float(hedge_cooldown)
        self.max_gross_vol = float(max_gross_vol) if max_gross_vol is not None else None

        self.hedge_vol_lookback = int(hedge_vol_lookback)
        self.hedge_vol_window = int(hedge_vol_window)
        self.hedge_vol_quantile = float(hedge_vol_quantile)
        self.hedge_vol_base = int(hedge_vol_base)
        self.hedge_vol_mult = float(hedge_vol_mult)

        self.be_trigger_steps = int(be_trigger_steps)
        self.be_buffer_points = int(be_buffer_points)

        # hedge runtime
        self._last_hedge_time = 0.0
        self._last_hedge_entry_price = None

        # cache rates (减少 copy_rates 压力)
        
        # 内部状态变量
        self._last_atr_value = None
        self._last_atr_time = 0
        self._last_tick_time = 0
        self._last_adapt_bar_time = 0
        
        # 日志相关
        self._last_status_log_time = 0
        self._status_log_interval = 60 # 默认60秒打印一次状态
        
        # 统计分析相关属性
        self._stats = {
            'magic': self.magic,
            'start_time': time.time(),
            'last_reset_time': time.time(),
            'long_profitable_count': 0,
            'long_profitable_amount': 0.0,
            'short_profitable_count': 0,
            'short_profitable_amount': 0.0,
            'last_stats_update_time': 0
        }

        # Incremental stats tracking (per-order profit).
        self._order_profit = {}
        self._order_type = {}
        self._last_deal_time = self._stats['last_reset_time']
        self._last_deal_ticket = 0
        
        # [优化] 缓存静态 Symbol 信息
        self._cache_symbol_info()

    def _normalize_mode(self, mode):
        normalized = str(mode or "neutral").strip().lower()
        if normalized not in self._VALID_MODES:
            Logger.log(self.symbol, "WARN", f"Invalid mode '{mode}', fallback to neutral")
            return "neutral"
        return normalized

    def _mt5_call(self, func, *args, **kwargs):
        """Wrap an MT5 API call with the shared lock when available."""
        if self.lock:
            with self.lock:
                return func(*args, **kwargs)
        return func(*args, **kwargs)

    def get_state(self):
        """获取策略内部状态，用于配置同步时保持状态"""
        return {
            'pause_until': self.pause_until,
            'enabled': self.enabled,
            '_last_atr_value': self._last_atr_value,
            '_last_tick_time': self._last_tick_time,
            '_last_atr_time': self._last_atr_time,
            'anchor': self.anchor,
            '_last_recenter_time': self._last_recenter_time,
            "_last_hedge_time": self._last_hedge_time,
            "_last_hedge_entry_price": self._last_hedge_entry_price,
            '_stats': self._stats
        }

    def set_state(self, state):
        """恢复策略内部状态"""
        if state:
            self.pause_until = state.get('pause_until', self.pause_until)
            self.enabled = state.get('enabled', self.enabled)
            self._last_atr_value = state.get('_last_atr_value', self._last_atr_value)
            self._last_tick_time = state.get('_last_tick_time', self._last_tick_time)
            self._last_atr_time = state.get('_last_atr_time', self._last_atr_time)
            self.anchor = state.get('anchor', self.anchor)
            self._last_recenter_time = state.get('_last_recenter_time', self._last_recenter_time)
            self._last_hedge_time = float(state.get("_last_hedge_time", self._last_hedge_time) or 0.0)
            self._last_hedge_entry_price = state.get("_last_hedge_entry_price", self._last_hedge_entry_price)
            # 恢复统计数据
            if '_stats' in state:
                self._stats = state['_stats']

    def _deal_net_profit(self, deal) -> float:
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        return profit + swap + commission

    def _deal_time_value(self, deal) -> float:
        t = getattr(deal, "time", 0)
        if hasattr(t, "timestamp"):
            try:
                return float(t.timestamp())
            except Exception:
                pass
        try:
            return float(t)
        except Exception:
            return 0.0

    def _adjust_profitable_stats(self, order_type: str, amount_delta: float, count_delta: int = 0) -> None:
        if order_type == "long":
            self._stats['long_profitable_amount'] += amount_delta
            self._stats['long_profitable_count'] += count_delta
        elif order_type == "short":
            self._stats['short_profitable_amount'] += amount_delta
            self._stats['short_profitable_count'] += count_delta

    def _apply_deal_to_stats(self, deal) -> None:
        order_ticket = getattr(deal, "order", None)
        if order_ticket is None:
            return

        delta = self._deal_net_profit(deal)
        prev_total = float(self._order_profit.get(order_ticket, 0.0) or 0.0)

        order_type = self._order_type.get(order_ticket)
        type_was_known = order_type is not None
        if order_type is None:
            if deal.type == mt5.DEAL_TYPE_BUY:
                order_type = "long"
            elif deal.type == mt5.DEAL_TYPE_SELL:
                order_type = "short"
            if order_type:
                self._order_type[order_ticket] = order_type

        was_positive = prev_total > 0.0
        if not type_was_known:
            was_positive = False

        new_total = prev_total + delta
        self._order_profit[order_ticket] = new_total
        is_positive = new_total > 0.0

        if not order_type:
            return

        if was_positive:
            if is_positive:
                self._adjust_profitable_stats(order_type, delta, 0)
            else:
                self._adjust_profitable_stats(order_type, -prev_total, -1)
        else:
            if is_positive:
                self._adjust_profitable_stats(order_type, new_total, 1)

    def _update_stats(self):
        """Update stats incrementally from new deals."""
        now = time.time()
        # Limit update frequency to avoid heavy MT5 calls.
        if now - self._stats['last_stats_update_time'] < 300:  # 5 min
            return

        try:
            start_time = self._last_deal_time if self._last_deal_time else self._stats['last_reset_time']
            deals = self._mt5_call(mt5.history_deals_get, symbol=self.symbol, group="*", start=start_time)

            if deals:
                max_time = float(self._last_deal_time or 0.0)
                max_ticket = int(self._last_deal_ticket or 0)
                for deal in deals:
                    if deal.magic != self.magic:
                        continue

                    deal_time = self._deal_time_value(deal)
                    deal_ticket = int(getattr(deal, "ticket", 0) or 0)
                    if (deal_time < self._last_deal_time) or (
                        deal_time == self._last_deal_time and deal_ticket <= self._last_deal_ticket
                    ):
                        continue

                    self._apply_deal_to_stats(deal)

                    if (deal_time > max_time) or (deal_time == max_time and deal_ticket > max_ticket):
                        max_time = deal_time
                        max_ticket = deal_ticket

                self._last_deal_time = max_time
                self._last_deal_ticket = max_ticket

            self._stats['last_stats_update_time'] = now

        except Exception as e:
            Logger.log(self.symbol, "EXCEPTION", f"鏇存柊缁熻鏁版嵁寮傚父: {str(e)}")

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

    def _get_tick(self):
        return self._mt5_call(mt5.symbol_info_tick, self.symbol)

    def _is_market_open(self, tick=None):
        """检查市场是否开放 (基于 Tick 时间)"""
        if tick is None:
            tick = self._get_tick()
                
        if not tick: return False
        # 如果最后一次 Tick 距离现在超过 10 分钟 (600秒)，认为休市
        if abs(time.time() - tick.time) > 600:
            return False
        return True

    def _prepare_request(self, request):
        if request is None:
            return None
        if not isinstance(request, dict):
            return request
        action = request.get("action")
        if action not in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING):
            return request
        if "type_filling" in request:
            return request
        allowed = (
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_RETURN,
        )
        if self.filling_mode in allowed:
            req = dict(request)
            req["type_filling"] = self.filling_mode
            return req
        return request

    def _queue_action(self, request):
        if self._action_collector is None:
            return False
        request = self._prepare_request(request)
        if request is not None:
            self._action_collector.append(request)
        return True

    def _dispatch_request(self, request):
        request = self._prepare_request(request)
        if self._action_collector is not None:
            if request is not None:
                self._action_collector.append(request)
            return _QueuedResult()
        return self._mt5_call(mt5.order_send, request)

    def _order_check(self, request):
        request = self._prepare_request(request)
        if request is None:
            return None
        try:
            result = self._mt5_call(mt5.order_check, request)
        except Exception as exc:
            Logger.log(self.symbol, "ERROR", f"order_check exception: {exc}")
            return None

        if result is None:
            last_error = mt5.last_error()
            Logger.log(self.symbol, "ERROR", f"order_check returned None. Error: {last_error}")
            return None

        retcode = getattr(result, "retcode", None)
        comment = getattr(result, "comment", "") or ""
        msg = (
            f"Check OK | RetCode={retcode} {comment} | "
            f"Type={request.get('type')} Price={request.get('price')} "
            f"Vol={request.get('volume')} Fill={request.get('type_filling', '')}"
        )
        Logger.log(self.symbol, "ORDER_CHECK", msg)
        return result

    def _send_with_fillings(self, request):
        candidates = []
        default = self.filling_mode
        allowed = (
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_RETURN,
        )
        if default in allowed:
            candidates.append(default)
        for mode in (mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK):
            if mode not in candidates:
                candidates.append(mode)

        last_result = None
        for mode in candidates:
            req = dict(request)
            req['type_filling'] = mode
            self._order_check(req)
            last_result = self._dispatch_request(req)
            if last_result is None:
                return None
            if last_result.retcode != 10030:
                return last_result

        self._order_check(request)
        return self._dispatch_request(request)

    def _index_orders(self, my_orders):
        self.bid_orders = {}
        self.ask_orders = {}
        for o in my_orders:
            if o.type == mt5.ORDER_TYPE_BUY_LIMIT:
                op = self._normalize_price(o.price_open)
                self.bid_orders.setdefault(op, []).append(o)
            elif o.type == mt5.ORDER_TYPE_SELL_LIMIT:
                op = self._normalize_price(o.price_open)
                self.ask_orders.setdefault(op, []).append(o)

    def _place_limit_order(self, side: str, price: float):
        try:
            is_buy = side == "buy"
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
            label = "BUY" if is_buy else "SELL"

            price = self._normalize_price(price)
            tp = self._normalize_price(price + self.tp_dist if is_buy else price - self.tp_dist)
            sl = None
            if self.sl_dist and self.sl_dist > 0:
                dist = max(self.sl_dist, self.stop_level, self.point)
                sl = price - dist if is_buy else price + dist
                sl = self._normalize_price(sl)
                if (is_buy and sl >= price) or ((not is_buy) and sl <= price):
                    sl = None
            vol = self._normalize_volume(self.lot)
            atr_coef = 1.0
            if self.use_atr and self.base_step:
                atr_coef = self.step / self.base_step
            price_width = max(12, self.digits + 9)

            sl_str = f"{sl:>{price_width}.{self.digits}f}" if sl is not None else f"{'--':>{price_width}}"

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": vol,
                "type": order_type,
                "price": price,
                "tp": tp,
                "deviation": 20,
                "magic": self.magic,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            if sl is not None:
                request["sl"] = sl

            if self._action_collector is not None:
                self._queue_action(request)
                Logger.log(
                    self.symbol,
                    "ORDER_SENT",
                    f"{label} LIMIT | Price={price:>{price_width}.{self.digits}f} TP={tp:>{price_width}.{self.digits}f} SL={sl_str} | Magic={self.magic:04d} | ATR={atr_coef:.2f}x (queued)",
                )
                return True

            result = self._send_with_fillings(request)
            if result is None:
                last_error = mt5.last_error()
                Logger.log(self.symbol, "ERROR", f"order_send returned None. Error: {last_error}")
                return None

            if result.retcode not in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                if result.retcode == 10004:  # REQUOTE
                    Logger.log(self.symbol, "WARN", "Requote, retrying...")
                    time.sleep(0.1)
                    result = self._send_with_fillings(request)
                    if result is None:
                        last_error = mt5.last_error()
                        Logger.log(self.symbol, "ERROR", f"order_send returned None after requote. Error: {last_error}")
                        return None
                    if result.retcode in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                        Logger.log(
                            self.symbol,
                            "ORDER_SENT",
                            f"{label} LIMIT | Price={price:>{price_width}.{self.digits}f} TP={tp:>{price_width}.{self.digits}f} SL={sl_str} | Magic={self.magic:04d} | ATR={atr_coef:.2f}x (retry)",
                        )
                        return result.order

                self._handle_order_error(result.retcode, getattr(result, "comment", ""), price)
                return None

            Logger.log(
                self.symbol,
                "ORDER_SENT",
                f"{label} LIMIT | Price={price:>{price_width}.{self.digits}f} TP={tp:>{price_width}.{self.digits}f} SL={sl_str} | Magic={self.magic:04d} | ATR={atr_coef:.2f}x",
            )
            return result.order

        except Exception as exc:
            Logger.log(self.symbol, "EXCEPTION", f"order exception: {exc}")
            self.pause_until = max(self.pause_until, time.time() + 2)
            return None

    def _place_buy_order(self, price):
        return self._place_limit_order("buy", price)

    def _place_sell_order(self, price):
        return self._place_limit_order("sell", price)

    def _handle_order_error(self, retcode, comment, price):
        """统一处理订单错误"""
        if retcode == 10018: # MARKET_CLOSED
            Logger.log(self.symbol, "SLEEP", "市场休市，暂停运行 5 分钟")
            self.pause_until = time.time() + 300
        elif retcode == 10017: # TRADE_DISABLED
            Logger.log(self.symbol, 'WARN', 'Trade disabled. Check terminal/account/symbol permissions.')
            self.pause_until = time.time() + 60
        elif retcode == 10027: # CLIENT_DISABLES_AT
            Logger.log(self.symbol, "CRITICAL", "MT5 终端 '自动交易' (Algo Trading) 未开启！请在 MT5 软件上方点击 'Algo Trading' 按钮。")
            self.enabled = False # 必须停止，否则会死循环
        elif retcode == 10004: # REQUOTE
            Logger.log(self.symbol, "WARN", "价格重新报价 (Requote)，稍后重试")
            self.pause_until = time.time() + 1
        elif retcode == 10013: # INVALID_REQUEST
            Logger.log(self.symbol, "ERROR", "无效请求参数")
            self.enabled = False # 致命错误，停止策略
        elif retcode == 10014: # INVALID_VOLUME
            Logger.log(self.symbol, "ERROR", "无效手数")
            self.enabled = False
        else:
            Logger.log(self.symbol, "ORDER_FAIL", f"RetCode={retcode} | Price={price:.{self.digits}f} | Reason: {comment}")
            # 通用错误暂停 5 秒，防止刷屏
            self.pause_until = time.time() + 5

    def clear_old_orders(self):
        """启动时清理旧网格挂单"""
        orders = self._mt5_call(mt5.orders_get, symbol=self.symbol)
            
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    res = self._dispatch_request({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    if res is None:
                        continue
                    if res.retcode == 10018: # MARKET_CLOSED
                        Logger.log(self.symbol, "WARN", "市场休市，无法撤单，暂停运行 5 分钟")
                        self.pause_until = time.time() + 300
                        return
            Logger.log(self.symbol, "CLEANUP", "历史挂单已清理")

    # ------------------------
    # Risk / caps helpers
    # ------------------------
    def _calc_exposure(self, my_positions, my_orders):
        """计算当前持仓和挂单的敞口情况。
        
        Args:
            my_positions: 本策略的持仓列表
            my_orders: 本策略的挂单列表
            
        Returns:
            tuple: (long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol)
        """
        # 持仓量计算
        long_vol = sum(p.volume for p in my_positions if p.type == mt5.POSITION_TYPE_BUY)
        short_vol = sum(p.volume for p in my_positions if p.type == mt5.POSITION_TYPE_SELL)

        # 挂单量计算 - 使用 volume_current（当前剩余量）而非 volume_initial（初始量）
        # 因为部分成交的订单应该只计算剩余部分
        pending_buy_vol = sum(
            getattr(o, 'volume_current', o.volume_initial) 
            for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT
        )
        pending_sell_vol = sum(
            getattr(o, 'volume_current', o.volume_initial) 
            for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT
        )

        # 净持仓 = (多头 + 待买) - (空头 + 待卖)
        net_vol = (long_vol + pending_buy_vol) - (short_vol + pending_sell_vol)

        return long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol

    def _allow_side(self, side, long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol,
                     *, long_pos_count: int = 0, short_pos_count: int = 0):
        _ = net_vol  # Keep signature compatibility with legacy call sites.
        return self.risk_manager.check_inventory_limits(
            long_vol=long_vol,
            short_vol=short_vol,
            pending_buy_vol=pending_buy_vol,
            pending_sell_vol=pending_sell_vol,
            net_vol=net_vol,
            lot=self.lot,
            side=side,
            mode=self.mode,
            max_net_vol=self.max_net_vol,
            max_long_vol=self.max_long_vol,
            max_short_vol=self.max_short_vol,
            max_long_pos=self.max_long_pos,
            max_short_pos=self.max_short_pos,
            long_pos_count=long_pos_count,
            short_pos_count=short_pos_count,
            hedge_enabled=self.hedge_enabled,
        )

    def _has_duplicate_position_level(self, price, pos_k_set, existing_positions_prices):
        if pos_k_set:
            level = round((price - self.anchor) / self.step)
            return level in pos_k_set

        for p_price in existing_positions_prices:
            if abs(p_price - price) < (self.step * 0.1):
                return True
        return False

    def _place_side_targets(
        self,
        *,
        side: str,
        targets,
        existing_prices,
        market_price: float,
        min_dist: float,
        pos_k_set,
        existing_positions_prices,
        long_vol: float,
        short_vol: float,
        pending_buy_vol: float,
        pending_sell_vol: float,
        net_vol: float,
        long_pos_count: int,
        short_pos_count: int,
        placed_count: int,
    ):
        skip_exist = 0
        skip_near = 0
        skip_pos = 0
        skip_cap = 0
        skip_risk = 0
        placed_side = 0

        for price in targets:
            if placed_count >= self.max_new_orders_per_update:
                skip_cap += 1
                break
            if price in existing_prices:
                skip_exist += 1
                continue
            if abs(price - market_price) < min_dist:
                skip_near += 1
                continue

            if self._has_duplicate_position_level(price, pos_k_set, existing_positions_prices):
                skip_pos += 1
                continue

            if not self._allow_side(
                side,
                long_vol,
                short_vol,
                pending_buy_vol,
                pending_sell_vol,
                net_vol,
                long_pos_count=long_pos_count,
                short_pos_count=short_pos_count,
            ):
                skip_risk += 1
                break

            placed = self._place_buy_order(price) if side == "buy" else self._place_sell_order(price)
            if placed:
                placed_count += 1
                placed_side += 1
                if side == "buy":
                    pending_buy_vol += self.lot
                    net_vol += self.lot
                else:
                    pending_sell_vol += self.lot
                    net_vol -= self.lot

        return {
            "placed_count": placed_count,
            "placed_side": placed_side,
            "pending_buy_vol": pending_buy_vol,
            "pending_sell_vol": pending_sell_vol,
            "net_vol": net_vol,
            "skip_exist": skip_exist,
            "skip_near": skip_near,
            "skip_pos": skip_pos,
            "skip_cap": skip_cap,
            "skip_risk": skip_risk,
        }

    # ------------------------
    # Hedge Helpers
    # ------------------------
    def _get_m1_rates_cached(self, n: int = 450, cache_sec: int = 10):
        if self.datafeed is not None:
            return self.datafeed.get_rates(
                self.symbol,
                mt5.TIMEFRAME_M1,
                n,
                cache_seconds=cache_sec,
                min_ratio=0.7,
            )

        rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, mt5.TIMEFRAME_M1, 0, n)

        if rates is None or len(rates) < int(n * 0.7):
            return None

        return rates

    @staticmethod
    def _quantile(arr, q: float):
        if not arr:
            return None
        return float(np.quantile(arr, np.clip(q, 0.0, 1.0)))

    def _volatility_gate(self, rates):
        # range-based vol: high-low
        lb = self.hedge_vol_lookback
        win = self.hedge_vol_window
        q = self.hedge_vol_quantile
        r = rates[-lb:] if len(rates) >= lb else rates
        ranges = [float(x["high"] - x["low"]) for x in r]
        if len(ranges) < win + 10:
            return False, None, None
        cur = sum(ranges[-win:]) / win
        thr = self._quantile(ranges, q)
        return (thr is not None and cur >= thr), cur, thr

    def _volume_gate(self, rates):
        base = self.hedge_vol_base
        win = self.hedge_vol_window
        mult = self.hedge_vol_mult
        v = [float(x["tick_volume"]) for x in rates]
        if len(v) < base + win + 10:
            return False, None, None
        cur = sum(v[-win:]) / win
        basev = sum(v[-(base + win):-win]) / base
        if basev <= 0:
            return False, cur, basev
        return cur >= mult * basev, cur, basev

    def _open_hedge_sell(self, vol, tick=None):
        if tick is None:
            tick = self._get_tick()

        if tick is None:
            return None
        vol = self._normalize_volume(vol)
        price = self._normalize_price(tick.bid)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": vol,
            "type": mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 20,
            "magic": self.magic,
            "comment": "HEDGE_SELL",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        
        return self._send_with_fillings(req)

    def _close_sell_position(self, pos_ticket, vol, tick=None):
        if tick is None:
            tick = self._get_tick()

        if tick is None:
            return None
        vol = self._normalize_volume(vol)
        price = self._normalize_price(tick.ask)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": int(pos_ticket),
            "volume": vol,
            "type": mt5.ORDER_TYPE_BUY,  # BUY 平空
            "price": price,
            "deviation": 20,
            "magic": self.magic,
            "comment": "HEDGE_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        
        return self._send_with_fillings(req)

    def _move_sell_sl_to_breakeven(self, pos, tick=None):
        if tick is None:
            tick = self._get_tick()

        if tick is None:
            return None

        # 空单盈利条件：ask < open
        if tick.ask >= pos.price_open:
            return None

        sl = pos.price_open + self.be_buffer_points * self.point
        sl = self._normalize_price(sl)

        # 若已有SL更紧（更低），不动；若无SL或更松，则更新
        if pos.sl and float(pos.sl) <= float(sl):
            return None

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": int(pos.ticket),
            "sl": sl,
            "tp": pos.tp,
            "magic": self.magic,
            "comment": "HEDGE_BE",
        }
        
        return self._dispatch_request(req)

    def _run_hedge_manager(self, my_positions, tick):
        """Execute the hedge manager logic for long-mode strategies.

        Handles break-even stop adjustment, entry gating, tranche entry/exit.
        Extracted from update() for readability; called only when hedge_enabled,
        mode == "long", and max_net_vol is configured.
        """
        long_pos = [p for p in my_positions if p.type == mt5.POSITION_TYPE_BUY]
        short_pos = [p for p in my_positions if p.type == mt5.POSITION_TYPE_SELL]

        long_vol = sum(p.volume for p in long_pos)
        short_vol = sum(p.volume for p in short_pos)
        net_vol = long_vol - short_vol

        cap = float(self.max_net_vol)
        hedge_target = cap * self.hedge_fraction
        tranche = hedge_target / max(1, self.hedge_tranches)

        gross = long_vol + short_vol
        if self.max_gross_vol is not None and gross >= self.max_gross_vol:
            return  # total position at cap

        now = time.time()
        mid = (tick.bid + tick.ask) / 2.0

        # (A) Break-even stop for profitable hedges
        be_trigger = self.be_trigger_steps * self.step
        for pos in short_pos:
            if (pos.price_open - tick.ask) >= be_trigger:
                self._move_sell_sl_to_breakeven(pos, tick=tick)

        # (B) Entry gates: volatility + volume
        rates = self._get_m1_rates_cached(n=450, cache_sec=10)
        vol_ok, vol_cur, vol_thr = False, None, None
        volm_ok, v_cur, v_base = False, None, None
        if rates is not None:
            vol_ok, vol_cur, vol_thr = self._volatility_gate(rates)
            volm_ok, v_cur, v_base = self._volume_gate(rates)
        gate_ok = vol_ok and volm_ok

        # (C) Tranche entry
        if net_vol >= cap and short_vol < hedge_target:
            if gate_ok and (now - self._last_hedge_time >= self.hedge_cooldown):
                ok_move = (
                    self._last_hedge_entry_price is None
                    or mid <= self._last_hedge_entry_price - self.hedge_entry_steps * self.step
                )
                if ok_move:
                    vol_to_add = min(tranche, hedge_target - short_vol)
                    if self.max_gross_vol is None or (gross + vol_to_add <= self.max_gross_vol):
                        res = self._open_hedge_sell(vol_to_add, tick=tick)
                        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            self._last_hedge_time = now
                            self._last_hedge_entry_price = mid
                            Logger.log(
                                self.symbol,
                                "HEDGE_ADD",
                                f"Magic={self.magic:04d} | Add={vol_to_add:6.2f} Short={short_vol:6.2f}/{hedge_target:6.2f} "
                                f"Net={net_vol:6.2f}/{cap:6.2f} | Volatility={vol_cur:6.3f}>={vol_thr:6.3f} "
                                f"Volume={v_cur:6.1f}>={self.hedge_vol_mult}x{v_base:6.1f}",
                            )

        # (D) Tranche exit on rebound / safe zone
        safe_net = cap * (1.0 - self.hedge_fraction)
        rebound = (
            self._last_hedge_entry_price is not None
            and mid >= self._last_hedge_entry_price + self.hedge_exit_steps * self.step
        )
        if short_vol > 0 and (now - self._last_hedge_time >= self.hedge_cooldown):
            if rebound or net_vol <= safe_net:
                target_pos = min(short_pos, key=lambda p: p.ticket)
                vol_to_close = min(tranche, target_pos.volume)
                res = self._close_sell_position(target_pos.ticket, vol_to_close, tick=tick)
                if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    self._last_hedge_time = now
                    Logger.log(
                        self.symbol,
                        "HEDGE_EXIT",
                        f"Magic={self.magic:04d} | Close={vol_to_close:6.2f} Net={net_vol:6.2f} "
                        f"SafeLevel={safe_net:6.2f} | Rebound={rebound}",
                    )

    def on_tick(self, ctx, *, action_collector=None):
        return self.update(
            orders_list=ctx.orders,
            positions_list=ctx.positions,
            tick=ctx.tick,
            orders_filtered=True,
            positions_filtered=True,
            atr=ctx.atr,
            action_collector=action_collector,
        )

    def update(
        self,
        orders_list=None,
        positions_list=None,
        tick=None,
        *,
        orders_filtered: bool = False,
        positions_filtered: bool = False,
        atr: float | None = None,
        action_collector: list | None = None,
    ):
        """核心巡检逻辑：支持双向网格与对标交易所模式"""
        self._action_collector = action_collector
        if not self.enabled:
            return
            
        # 休市暂停检查 (Error Backoff)
        now = time.time()
        if now < self.pause_until:
            return

        # 获取一次 tick，后续复用（Runner 可传入 tick，减少重复的 MT5 调用）
        if tick is None:
            tick = self._get_tick()
            
            
        if not tick or tick.bid <= 0: 
            self.pause_until = now + 5
            return

        # 市场活跃度检查 (Proactive Check)
        if not self._is_market_open(tick):
            return

        # 极端点差闸门 (Fuse)
        spread_check = self.risk_manager.check_spread(
            bid=tick.bid,
            ask=tick.ask,
            max_spread_points=self.max_spread_points,
            point=self.point,
            extreme_cooldown=self.extreme_cooldown,
            now=now,
        )
        if spread_check.triggered:
            Logger.log(
                self.symbol,
                "FUSE",
                f"Spread Exceeded | Current={spread_check.spread/self.point:6.1f}pt > Max={self.max_spread_points:6.1f}pt | Cooldown={self.extreme_cooldown}s",
            )
            self.pause_until = spread_check.pause_until
            return

        self._maybe_adapt_params()

        # --- ATR adaptive step/tp ---
        atr_value = None
        if self.use_atr and not self.adaptive_enabled:
            if atr is not None:
                atr_value = atr
                self._last_atr_value = float(atr_value)
                self._last_atr_time = now
            elif self.datafeed is not None:
                atr_value = self.datafeed.get_atr(
                    self.symbol,
                    self._resolve_timeframe(),
                    self.atr_period,
                    self.atr_mode,
                    self.atr_smooth,
                    self.atr_update_seconds,
                )
                if atr_value is not None:
                    self._last_atr_value = float(atr_value)
                    self._last_atr_time = now
            else:
                atr_value = self._calculate_atr()

        if self.use_atr and not self.adaptive_enabled and atr_value:
            self._apply_atr_targets(float(atr_value))

        mid_price = (tick.bid + tick.ask) / 2
        
        # 边界检查
        range_action = self.risk_manager.check_range(
            mid_price=mid_price,
            min_price=self.min_price,
            max_price=self.max_price,
            out_of_range_action=self.out_of_range_action,
        )
        if range_action == RangeAction.STOP:
            Logger.log(self.symbol, "STOP", f"mid {mid_price} out of range [{self.min_price}, {self.max_price}]")
            self.enabled = False
            self.clear_old_orders()
            return
        if range_action == RangeAction.FREEZE:
            # freeze means do nothing (no trim/no add)
            return

        # 1. 获取当前属于本实例的挂单和持仓
        if orders_list is not None:
            if orders_filtered:
                my_orders = orders_list
            else:
                # 过滤属于本策略的订单 (增加 symbol 过滤)
                my_orders = [o for o in orders_list if o.magic == self.magic and o.symbol == self.symbol]
        else:
            orders = self._mt5_call(mt5.orders_get, symbol=self.symbol)
            my_orders = [o for o in orders if o.magic == self.magic] if orders else []
        
        # 1.5 获取持仓
        if positions_list is not None:
            if positions_filtered:
                my_positions = positions_list
            else:
                # 过滤属于本策略的持仓 (增加 symbol 过滤)
                my_positions = [p for p in positions_list if p.symbol == self.symbol and p.magic == self.magic]
        else:
            positions = self._mt5_call(mt5.positions_get, symbol=self.symbol)
            # 增加 magic 过滤
            my_positions = [p for p in positions if p.symbol == self.symbol and p.magic == self.magic] if positions else []

        self._index_orders(my_orders)
            
        # --- 状态播报 (每分钟一次) ---
        should_log_status = time.time() - self._last_status_log_time > self._status_log_interval
        if should_log_status:
            float_profit = sum(p.profit for p in my_positions)
            pos_vol = sum(p.volume for p in my_positions)
            buy_orders = sum(len(v) for v in self.bid_orders.values())
            sell_orders = sum(len(v) for v in self.ask_orders.values())
            
            # 更新统计数据
            self._update_stats()
            
            price_width = max(12, self.digits + 8)
            step_width = max(8, self.digits + 4)
            step_prec = max(1, int(self.digits))
            atr_coef = 1.0
            if self.use_atr and self.base_step:
                atr_coef = self.step / self.base_step
            
            # 优化后的状态日志：清晰的字段标签 + 统一对齐
            status_msg = (
                f"Magic={self.magic:04d} | "
                f"Price: {tick.bid:>{price_width}.{self.digits}f} / {tick.ask:>{price_width}.{self.digits}f} | "
                f"Position: {len(my_positions):2d}pos {pos_vol:6.2f}lot PnL:{float_profit:+10.2f} | "
                f"Orders: Buy={buy_orders:2d} Sell={sell_orders:2d} | "
                f"Grid: Step={self.step:>{step_width}.{step_prec}f} ATR={atr_coef:4.2f}x | "
                f"Stats: Long={self._stats['long_profitable_count']:3d}cnt/{self._stats['long_profitable_amount']:+10.2f} "
                f"Short={self._stats['short_profitable_count']:3d}cnt/{self._stats['short_profitable_amount']:+10.2f}"
            )
            Logger.log(self.symbol, "STATUS", status_msg)
            self._last_status_log_time = time.time()

        # --- Fixed Grid: Initialize anchor to min_price once ---
        if self.anchor is None:
             self.anchor = self.min_price
        
        # Disable dynamic recentering for Fixed Grid mode
        # self._init_anchor_if_needed(mid_price)
        # self._maybe_recenter(mid_price)

        # ========== HEDGE MANAGER ==========
        if self.hedge_enabled and self.mode == "long" and self.max_net_vol is not None:
            self._run_hedge_manager(my_positions, tick)
        # ========== END HEDGE MANAGER ==========

        positions_for_block = my_positions
        if self.mode == "long":
            positions_for_block = [p for p in my_positions if p.type == mt5.POSITION_TYPE_BUY]
        elif self.mode == "short":
            positions_for_block = [p for p in my_positions if p.type == mt5.POSITION_TYPE_SELL]

        existing_positions_prices = {self._normalize_price(p.price_open) for p in positions_for_block}
        pos_k_set = set()
        if self.step > 0 and self.anchor is not None:
            pos_k_set = {round((p_price - self.anchor) / self.step) for p_price in existing_positions_prices}

        min_dist = max(self.stop_level, self.point * 10) # 最小挂单距离

        # 2. 生成目标网格层级 (围绕 Anchor 固定生成)
        target_buys, target_sells = self.grid_calculator.build_targets(
            anchor=self.anchor,
            step=self.step,
            min_price=self.min_price,
            max_price=self.max_price,
            bid=tick.bid,
            ask=tick.ask,
            buy_window=self.buy_window,
            sell_window=self.sell_window,
            mode=self.mode,
            recenter_steps=self.recenter_steps,
            min_dist=min_dist,
            blocked_k=pos_k_set,
        )

        if should_log_status and self.max_net_vol is not None and self.lot > 0:
            long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(
                my_positions, my_orders
            )
            # --- account stop-out buffer (爆仓金额) ---
            liq_buffer = None
            try:
                account = mt5.account_info()
                if account:
                    equity = float(getattr(account, "equity", 0.0) or 0.0)
                    margin = float(getattr(account, "margin", 0.0) or 0.0)
                    so_mode = getattr(account, "margin_so_mode", None)
                    so_level = getattr(account, "margin_so_so", None)
                    if so_level is not None:
                        # Stop-out equity threshold
                        if so_mode == getattr(mt5, "ACCOUNT_STOP_OUT_PERCENT", None):
                            stopout_equity = margin * float(so_level) / 100.0
                        else:
                            stopout_equity = float(so_level)
                        liq_buffer = equity - stopout_equity
            except Exception:
                liq_buffer = None
            cap = float(self.max_net_vol)
            side = None
            current = 0.0
            remaining = 0.0

            if self.mode == "long":
                side = "buy"
                current = long_vol + pending_buy_vol
                remaining = cap - current
            elif self.mode == "short":
                side = "sell"
                current = short_vol + pending_sell_vol
                remaining = cap - current
            else:
                if net_vol >= 0:
                    side = "buy"
                    current = net_vol
                    remaining = cap - net_vol
                else:
                    side = "sell"
                    current = -net_vol
                    remaining = cap + net_vol

            def _cap_cell(label: str, value: str, width: int) -> str:
                return Logger._pad_display(f"{label}{value}", width)

            remark = ""
            last_target = None
            diff = None

            if remaining <= 0:
                max_orders = 0
            else:
                max_orders = int((remaining / self.lot) + 1e-9)

            if max_orders > 0:
                targets = target_buys if side == "buy" else target_sells
                # [FIX] 无论 targets 是否足够，若 max_orders 很大，我们都尝试估算“末档价格”
                # 之前使用固定 Anchor 计算导致价格严重偏差（3797 vs 4900），现在改用现价推算
                
                is_window_limited = (len(targets) < max_orders)
                
                if is_window_limited and self.step > 0:
                    # 窗口不足，需要推算末档价
                    if side == "buy":
                        # 买单：向下推算。参考价为最近的一个买单（或现价）
                        ref_price = targets[0] if targets else tick.ask
                        last_target = self._normalize_price(ref_price - self.step * (max_orders - 1))
                    else:
                        # 卖单：向上推算。参考价为最近的一个卖单（或现价）
                        # target_sells 是降序 [High ... Low]，虽然我们是从 Low 开始挂，
                        # 但推算“最远”的那个价格时，应该是 Low + (N-1)*Step
                        ref_price = targets[-1] if targets else tick.bid
                        last_target = self._normalize_price(ref_price + self.step * (max_orders - 1))
                    
                    remark = "窗口限制"
                elif targets:
                    # 窗口足够覆盖资金上限
                    if side == "buy":
                        # 买单 [High ... Low]，取第 N 个
                        idx = min(len(targets), max_orders) - 1
                        last_target = targets[idx]
                    else:
                        # 卖单 [High ... Low]，我们是从 Low (targets[-1]) 开始成交的
                        # 所以如果有 N 个配额，对应的最远价格是 targets[-N]
                        idx = -min(len(targets), max_orders)
                        last_target = targets[idx]
                else:
                    remark = "无目标"

                if last_target is not None:
                    if side == "buy":
                        diff = tick.ask - last_target
                    else:
                        diff = last_target - tick.bid
                    if last_target < self.min_price or last_target > self.max_price:
                        remark = "超范围" if not remark else f"{remark},超范围"

            side_cn = "买" if side == "buy" else "卖"
            cur_price = tick.ask if side == "buy" else tick.bid
            cur_str = f"{cur_price:.{self.digits}f}"
            expect_str = f"{last_target:.{self.digits}f}" if last_target is not None else "--"
            
            diff_str = "--"
            pct_str = "--"
            step_ratio_str = "--"
            if diff is not None:
                pct = 0.0
                if cur_price > 0:
                    pct = (diff / cur_price) * 100
                diff_str = f"{diff:+.{self.digits}f}"
                pct_str = f"{pct:+.2f}%"

            if cur_price > 0 and self.step > 0:
                step_ratio = (self.step / cur_price) * 100
                step_ratio_str = f"{step_ratio:.2f}%"

            pct_label = "跌幅:" if side == "buy" else "涨幅:"

            remark_str = remark if remark else ""
            liq_str = "--" if liq_buffer is None else f"{liq_buffer:+.2f}"

            cells = [
                _cap_cell("方向:", side_cn, 8),
                _cap_cell("上限:", f"{cap:.2f}", 12),
                _cap_cell("当前:", f"{current:.2f}", 12),
                _cap_cell("剩余:", f"{max(0.0, remaining):.2f}", 12),
                _cap_cell("手数:", f"{self.lot:.2f}", 10),
                _cap_cell("单数:", f"{max_orders}", 8),
                _cap_cell("爆仓额:", liq_str, 12),
                _cap_cell("现价:", cur_str, 16),
                _cap_cell("末档价:", expect_str, 16),
                _cap_cell("差值:", diff_str, 22),
                _cap_cell(pct_label, pct_str, 12),
                _cap_cell("步长%:", step_ratio_str, 12),
                _cap_cell("备注:", remark_str, 10),
            ]

            cap_msg = f"magic={self.magic} | CAP | " + " | ".join(cells)
            Logger.log(self.symbol, "STATUS", cap_msg)
        
        # 3. 挂单维护逻辑
        
        # A. TRIM (清理多余/超界挂单)
        if self.auto_trim:
            target_set = set(target_buys + target_sells)
            removed_tickets = set()
            for o in list(my_orders):
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    op = self._normalize_price(o.price_open)
                    should_remove = False

                    if op not in target_set:
                        should_remove = True

                    if op < self.min_price or op > self.max_price:
                        should_remove = True

                    if o.type == mt5.ORDER_TYPE_BUY_LIMIT and self.mode == "short":
                        should_remove = True
                    if o.type == mt5.ORDER_TYPE_SELL_LIMIT and self.mode == "long":
                        should_remove = True

                    if should_remove:
                        res = self._dispatch_request({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            removed_tickets.add(o.ticket)
                            Logger.log(self.symbol, "TRIM", f"\u5b89\u5168\u64a4\u5355(\u8d8a\u754c/\u6a21\u5f0f\u51b2\u7a81/\u76ee\u6807\u5916): {op}")
            if removed_tickets:
                my_orders = [o for o in my_orders if o.ticket not in removed_tickets]
                self._index_orders(my_orders)

        # B. 补单 (带库存风控)
        
        # 统计库存
        long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(my_positions, my_orders)
        
        # 统计持仓数量（用于 max_long_pos / max_short_pos 检查）
        long_pos_count = sum(1 for p in my_positions if p.type == mt5.POSITION_TYPE_BUY)
        short_pos_count = sum(1 for p in my_positions if p.type == mt5.POSITION_TYPE_SELL)

        mode_conflict = False
        if self.mode == "long":
            if (not self.hedge_enabled) and short_pos_count > 0:
                mode_conflict = True
        elif self.mode == "short":
            if long_pos_count > 0:
                mode_conflict = True

        if mode_conflict and should_log_status:
            Logger.log(
                self.symbol,
                "WARN",
                f"mode={self.mode} with opposite positions; skip new orders",
            )

        existing_buy_prices = set(self.bid_orders.keys())
        existing_sell_prices = set(self.ask_orders.keys())
        placed_count = 0
        placed_buy = 0
        placed_sell = 0
        skip_buy_exist = 0
        skip_buy_near = 0
        skip_buy_pos = 0
        skip_buy_cap = 0
        skip_buy_risk = 0
        skip_sell_exist = 0
        skip_sell_near = 0
        skip_sell_pos = 0
        skip_sell_cap = 0
        skip_sell_risk = 0
        
        if not mode_conflict:
            buy_result = self._place_side_targets(
                side="buy",
                targets=target_buys,
                existing_prices=existing_buy_prices,
                market_price=tick.ask,
                min_dist=min_dist,
                pos_k_set=pos_k_set,
                existing_positions_prices=existing_positions_prices,
                long_vol=long_vol,
                short_vol=short_vol,
                pending_buy_vol=pending_buy_vol,
                pending_sell_vol=pending_sell_vol,
                net_vol=net_vol,
                long_pos_count=long_pos_count,
                short_pos_count=short_pos_count,
                placed_count=placed_count,
            )
            placed_count = buy_result["placed_count"]
            placed_buy = buy_result["placed_side"]
            pending_buy_vol = buy_result["pending_buy_vol"]
            pending_sell_vol = buy_result["pending_sell_vol"]
            net_vol = buy_result["net_vol"]
            skip_buy_exist = buy_result["skip_exist"]
            skip_buy_near = buy_result["skip_near"]
            skip_buy_pos = buy_result["skip_pos"]
            skip_buy_cap = buy_result["skip_cap"]
            skip_buy_risk = buy_result["skip_risk"]

            sell_result = self._place_side_targets(
                side="sell",
                targets=target_sells,
                existing_prices=existing_sell_prices,
                market_price=tick.bid,
                min_dist=min_dist,
                pos_k_set=pos_k_set,
                existing_positions_prices=existing_positions_prices,
                long_vol=long_vol,
                short_vol=short_vol,
                pending_buy_vol=pending_buy_vol,
                pending_sell_vol=pending_sell_vol,
                net_vol=net_vol,
                long_pos_count=long_pos_count,
                short_pos_count=short_pos_count,
                placed_count=placed_count,
            )
            placed_count = sell_result["placed_count"]
            placed_sell = sell_result["placed_side"]
            pending_buy_vol = sell_result["pending_buy_vol"]
            pending_sell_vol = sell_result["pending_sell_vol"]
            net_vol = sell_result["net_vol"]
            skip_sell_exist = sell_result["skip_exist"]
            skip_sell_near = sell_result["skip_near"]
            skip_sell_pos = sell_result["skip_pos"]
            skip_sell_cap = sell_result["skip_cap"]
            skip_sell_risk = sell_result["skip_risk"]

        if should_log_status:
            def _fmt_skip(label, exist, near, pos, cap, risk):
                parts = []
                if exist:
                    parts.append(f"exist={exist}")
                if near:
                    parts.append(f"near={near}")
                if pos:
                    parts.append(f"pos={pos}")
                if cap:
                    parts.append(f"cap={cap}")
                if risk:
                    parts.append(f"risk={risk}")
                if not parts:
                    return ""
                return f"{label}({', '.join(parts)})"

            skip_sections = [
                _fmt_skip("B", skip_buy_exist, skip_buy_near, skip_buy_pos, skip_buy_cap, skip_buy_risk),
                _fmt_skip("S", skip_sell_exist, skip_sell_near, skip_sell_pos, skip_sell_cap, skip_sell_risk),
            ]
            skip_sections = [s for s in skip_sections if s]
            if skip_sections:
                Logger.log(
                    self.symbol,
                    "SKIP",
                    f"magic={self.magic} | targets B:{len(target_buys)} S:{len(target_sells)} | "
                    f"placed B:{placed_buy} S:{placed_sell} | "
                    f"min_dist={min_dist:.{self.digits}f} | "
                    + " ".join(skip_sections),
                )
