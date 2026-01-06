# strategy_lib.py
import MetaTrader5 as mt5
import time
import numpy as np
from .logger import Logger

class GridStrategy:
    def __init__(self, symbol, step, tp_dist, lot, magic, 
                 window=6, min_p=0, max_p=999999, enabled=True, 
                 use_atr=False, atr_period=14, atr_factor=1.0,
                 mode="neutral", buy_window=None, sell_window=None, 
                 out_of_range_action="freeze", 
                 atr_update_seconds=5, atr_smooth=0.1, atr_change_threshold=0.01,
                 min_step_mult=0.5, max_step_mult=3.0,
                 lock=None,
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
        :param mode: "neutral" | "long" | "short"
        :param buy_window: 买单窗口大小 (默认等于 window)
        :param sell_window: 卖单窗口大小 (默认等于 window)
        :param out_of_range_action: "freeze" | "stop"
        """
        self.symbol = symbol
        self.base_step = float(step) # 保存初始步长
        self.step = float(step)
        self.tp_dist = float(tp_dist)
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
        
        # 新增参数
        self.mode = mode
        self.buy_window = buy_window if buy_window is not None else window
        self.sell_window = sell_window if sell_window is not None else window
        self.out_of_range_action = out_of_range_action
        
        # ATR 优化参数
        self.atr_update_seconds = atr_update_seconds
        self.atr_smooth = atr_smooth
        self.atr_change_threshold = atr_change_threshold
        self.min_step_mult = min_step_mult
        self.max_step_mult = max_step_mult
        
        self.lock = lock

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
        self._rates_cache_ts = 0.0
        self._rates_cache = None
        
        # 内部状态变量
        self._last_atr_value = None
        self._last_atr_time = 0
        self._last_tick_time = 0
        
        # 日志相关
        self._last_status_log_time = 0
        self._status_log_interval = 60 # 默认60秒打印一次状态
        
        # [优化] 缓存静态 Symbol 信息
        self._cache_symbol_info()

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

    def _cache_symbol_info(self):
        if self.lock:
            with self.lock:
                info = mt5.symbol_info(self.symbol)
        else:
            info = mt5.symbol_info(self.symbol)
            
        if info:
            self.digits = info.digits
            self.point = info.point
            self.stop_level = info.trade_stops_level * info.point
            self.vol_min = info.volume_min
            self.vol_max = info.volume_max
            self.vol_step = info.volume_step
            self.initialized = True
        else:
            self.digits = 2
            self.point = 0.01
            self.stop_level = 0
            self.vol_min = 0.01
            self.vol_max = 100
            self.vol_step = 0.01
            self.initialized = False
            Logger.log(self.symbol, "WARN", "初始化获取品种信息失败，使用默认值")

    def _normalize_price(self, price):
        return float(round(price, self.digits))

    def _normalize_volume(self, vol):
        # 简单的步长取整
        if self.vol_step > 0:
            steps = round(vol / self.vol_step)
            vol = steps * self.vol_step
        return float(round(max(self.vol_min, min(self.vol_max, vol)), 2))

    def _get_grid_level(self, price, anchor):
        """以 anchor 为锚点，把 price snap 到最近的网格线"""
        if self.step <= 0: return price
        k = round((price - anchor) / self.step)
        return anchor + k * self.step

    def _init_anchor_if_needed(self, mid_price):
        if self.anchor is None:
            # 用当前价格作为初始anchor，并snap到网格线上
            base0 = round(mid_price / self.step) * self.step
            self.anchor = self._normalize_price(base0)
            Logger.log(self.symbol, "INIT", f"初始化 Anchor: {self.anchor}")

    def _maybe_recenter(self, mid_price):
        """触发条件：偏离>=recenter_steps*step 且超过冷却时间"""
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
        Logger.log(self.symbol, "RECENTER", f"Anchor 平移 -> {self.anchor} (mid={mid_price:.2f})")
        return True

    def _calculate_atr(self):
        """计算 ATR (简单移动平均算法) - 向量化优化 + 平滑 + 缓存"""
        current_time = time.time()
        if current_time - self._last_atr_time < self.atr_update_seconds:
            return self._last_atr_value

        # 获取足够的数据: period + 1 根 K 线 (M15 周期)
        if self.lock:
            with self.lock:
                rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M15, 0, self.atr_period + 1)
        else:
            rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M15, 0, self.atr_period + 1)
            
        if rates is None or len(rates) < self.atr_period + 1:
            return None
            
        # 使用 numpy 向量化计算 (mt5 返回的是 numpy 结构化数组)
        high = rates['high'][1:]
        low = rates['low'][1:]
        close_prev = rates['close'][:-1]
        
        tr = np.maximum(high - low, np.abs(high - close_prev))
        tr = np.maximum(tr, np.abs(low - close_prev))
        
        raw_atr = np.mean(tr)
        
        # 平滑处理
        if self._last_atr_value is None:
            self._last_atr_value = raw_atr
        else:
            self._last_atr_value = (self._last_atr_value * (1 - self.atr_smooth)) + (raw_atr * self.atr_smooth)
            
        self._last_atr_time = current_time
        return self._last_atr_value

    def _is_market_open(self, tick=None):
        """检查市场是否开放 (基于 Tick 时间)"""
        if tick is None:
            if self.lock:
                with self.lock:
                    tick = mt5.symbol_info_tick(self.symbol)
            else:
                tick = mt5.symbol_info_tick(self.symbol)
                
        if not tick: return False
        # 如果最后一次 Tick 距离现在超过 10 分钟 (600秒)，认为休市
        if abs(time.time() - tick.time) > 600:
            return False
        return True

    def _place_buy_order(self, price):
        """内部方法：发送带止盈的买单"""
        try:
            # 使用缓存的 digits
            price = self._normalize_price(price)
            tp = self._normalize_price(price + self.tp_dist)
            vol = self._normalize_volume(self.lot)

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": vol,
                "type": mt5.ORDER_TYPE_BUY_LIMIT,
                "price": price,
                "tp": tp,
                "deviation": 20,  # 允许 20 点的滑点
                "magic": self.magic,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            
            if self.lock:
                with self.lock:
                    result = mt5.order_send(request)
            else:
                result = mt5.order_send(request)
            
            if result is None:
                last_error = mt5.last_error()
                Logger.log(self.symbol, "ERROR", f"下单返回 None. Error: {last_error}")
                return None

            # 填充模式兼容
            if result.retcode == 10030: 
                del request["type_filling"]
                if self.lock:
                    with self.lock:
                        result = mt5.order_send(request)
                else:
                    result = mt5.order_send(request)
                
                if result is None:
                    last_error = mt5.last_error()
                    Logger.log(self.symbol, "ERROR", f"下单返回 None (重试时). Error: {last_error}")
                    return None
            
            # 统一错误处理
            if result.retcode not in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                # 如果是价格变动 (Requote)，尝试重试一次
                if result.retcode == 10004: # REQUOTE
                    Logger.log(self.symbol, "WARN", "价格变动，正在重试...")
                    time.sleep(0.1)
                    # 重新获取价格并重试
                    if self.lock:
                        with self.lock:
                            tick = mt5.symbol_info_tick(self.symbol)
                    else:
                        tick = mt5.symbol_info_tick(self.symbol)
                        
                    if tick:
                        # 重新获取价格并重试 (这里其实应该重新计算 price，但为了简单重试原价)
                        if self.lock:
                            with self.lock:
                                result = mt5.order_send(request)
                        else:
                            result = mt5.order_send(request)
                        
                        if result is None:
                            last_error = mt5.last_error()
                            Logger.log(self.symbol, "ERROR", f"下单返回 None (Requote重试时). Error: {last_error}")
                            return None
                            
                        if result.retcode in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                            Logger.log(self.symbol, "ORDER_SENT", f"BUY LIMIT: {price:<10.2f} | TP: {tp:<10.2f} | Magic: {self.magic} (重试)")
                            return result.order

                self._handle_order_error(result.retcode, result.comment, price)
                return None
                
            Logger.log(self.symbol, "ORDER_SENT", f"BUY LIMIT: {price:<10.2f} | TP: {tp:<10.2f} | Magic: {self.magic}")
            return result.order
            
        except Exception as e:
            Logger.log(self.symbol, "EXCEPTION", f"下单异常: {str(e)}")
            # 异常时也进行退避，避免主循环频繁调用导致刷日志/高频重试
            self.pause_until = max(self.pause_until, time.time() + 2)
            return None

    def _place_sell_order(self, price):
        """内部方法：发送带止盈的卖单"""
        try:
            # 使用缓存的 digits
            price = self._normalize_price(price)
            tp = self._normalize_price(price - self.tp_dist)
            vol = self._normalize_volume(self.lot)

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": vol,
                "type": mt5.ORDER_TYPE_SELL_LIMIT,
                "price": price,
                "tp": tp,
                "deviation": 20,  # 允许 20 点的滑点
                "magic": self.magic,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            
            if self.lock:
                with self.lock:
                    result = mt5.order_send(request)
            else:
                result = mt5.order_send(request)
            
            if result is None:
                last_error = mt5.last_error()
                Logger.log(self.symbol, "ERROR", f"下单返回 None. Error: {last_error}")
                return None

            # 填充模式兼容
            if result.retcode == 10030: 
                del request["type_filling"]
                if self.lock:
                    with self.lock:
                        result = mt5.order_send(request)
                else:
                    result = mt5.order_send(request)
                
                if result is None:
                    last_error = mt5.last_error()
                    Logger.log(self.symbol, "ERROR", f"下单返回 None (重试时). Error: {last_error}")
                    return None
            
            # 统一错误处理
            if result.retcode not in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                # 如果是价格变动 (Requote)，尝试重试一次
                if result.retcode == 10004: # REQUOTE
                    Logger.log(self.symbol, "WARN", "价格变动，正在重试...")
                    time.sleep(0.1)
                    if self.lock:
                        with self.lock:
                            result = mt5.order_send(request)
                    else:
                        result = mt5.order_send(request)
                    
                    if result is None:
                        last_error = mt5.last_error()
                        Logger.log(self.symbol, "ERROR", f"下单返回 None (Requote重试时). Error: {last_error}")
                        return None
                        
                    if result.retcode in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                            Logger.log(self.symbol, "ORDER_SENT", f"SELL LIMIT: {price:<10.2f} | TP: {tp:<10.2f} | Magic: {self.magic} (重试)")
                            return result.order

                self._handle_order_error(result.retcode, result.comment, price)
                return None
                
            Logger.log(self.symbol, "ORDER_SENT", f"SELL LIMIT: {price:<10.2f} | TP: {tp:<10.2f} | Magic: {self.magic}")
            return result.order
            
        except Exception as e:
            Logger.log(self.symbol, "EXCEPTION", f"下单异常: {str(e)}")
            self.pause_until = max(self.pause_until, time.time() + 2)
            return None

    def _handle_order_error(self, retcode, comment, price):
        """统一处理订单错误"""
        if retcode == 10018: # MARKET_CLOSED
            Logger.log(self.symbol, "SLEEP", "市场休市，暂停运行 5 分钟")
            self.pause_until = time.time() + 300
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
            Logger.log(self.symbol, "ORDER_FAIL", f"RC: {retcode:<5} | 价格: {price:<10} | {comment}")
            # 通用错误暂停 5 秒，防止刷屏
            self.pause_until = time.time() + 5

    def clear_old_orders(self):
        """启动时清理旧网格挂单"""
        if self.lock:
            with self.lock:
                orders = mt5.orders_get(symbol=self.symbol)
        else:
            orders = mt5.orders_get(symbol=self.symbol)
            
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    if self.lock:
                        with self.lock:
                            res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    else:
                        res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                        
                    if res.retcode == 10018: # MARKET_CLOSED
                        Logger.log(self.symbol, "WARN", "市场休市，无法撤单，暂停运行 5 分钟")
                        self.pause_until = time.time() + 300
                        return
            Logger.log(self.symbol, "CLEANUP", "历史挂单已清理")

    # ------------------------
    # Risk / caps helpers
    # ------------------------
    def _calc_exposure(self, my_positions, my_orders):
        long_vol = sum(p.volume for p in my_positions if p.type == mt5.POSITION_TYPE_BUY)
        short_vol = sum(p.volume for p in my_positions if p.type == mt5.POSITION_TYPE_SELL)

        pending_buy_vol = sum(o.volume_current for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT)
        pending_sell_vol = sum(o.volume_current for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT)

        net_vol = (long_vol + pending_buy_vol) - (short_vol + pending_sell_vol)

        return long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol

    def _allow_side(self, side, long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol):
        """
        side: "buy" or "sell"
        mode handling:
          - neutral: abs(net) <= max_net_vol
          - long:    cap long exposure by max_net_vol
          - short:   cap short exposure by max_net_vol
        """
        if self.max_net_vol is None:
            return True

        cap = float(self.max_net_vol)

        if self.mode == "neutral":
            if side == "buy":
                # adding buy increases net
                return abs(net_vol + self.lot) <= cap
            else:
                # adding sell decreases net
                return abs(net_vol - self.lot) <= cap

        if self.mode == "long":
            # cap long exposure
            total_long = long_vol + pending_buy_vol
            if side == "buy":
                return (total_long + self.lot) <= cap
            else:
                return True  # allow sells (take profit / rebalance)

        if self.mode == "short":
            total_short = short_vol + pending_sell_vol
            if side == "sell":
                return (total_short + self.lot) <= cap
            else:
                return True

        return True

    # ------------------------
    # Hedge Helpers
    # ------------------------
    def _get_m1_rates_cached(self, n: int = 450, cache_sec: int = 10):
        now = time.time()
        if self._rates_cache is not None and (now - self._rates_cache_ts) < cache_sec:
            return self._rates_cache

        if self.lock:
            with self.lock:
                rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, n)
        else:
            rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, n)
            
        if rates is None or len(rates) < int(n * 0.7):
            return None

        self._rates_cache = rates
        self._rates_cache_ts = now
        return rates

    def _quantile(self, arr, q: float):
        # arr: list[float] non-empty
        xs = sorted(arr)
        if not xs:
            return None
        if q <= 0:
            return xs[0]
        if q >= 1:
            return xs[-1]
        pos = (len(xs) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        frac = pos - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

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

    def _open_hedge_sell(self, vol):
        if self.lock:
            with self.lock:
                tick = mt5.symbol_info_tick(self.symbol)
        else:
            tick = mt5.symbol_info_tick(self.symbol)
            
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
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        
        if self.lock:
            with self.lock:
                return mt5.order_send(req)
        else:
            return mt5.order_send(req)

    def _close_sell_position(self, pos_ticket, vol):
        if self.lock:
            with self.lock:
                tick = mt5.symbol_info_tick(self.symbol)
        else:
            tick = mt5.symbol_info_tick(self.symbol)
            
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
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        
        if self.lock:
            with self.lock:
                return mt5.order_send(req)
        else:
            return mt5.order_send(req)

    def _move_sell_sl_to_breakeven(self, pos):
        if self.lock:
            with self.lock:
                tick = mt5.symbol_info_tick(self.symbol)
        else:
            tick = mt5.symbol_info_tick(self.symbol)
            
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
        
        if self.lock:
            with self.lock:
                return mt5.order_send(req)
        else:
            return mt5.order_send(req)

    def update(self, orders_list=None, positions_list=None):
        """核心巡检逻辑：支持双向网格与对标交易所模式"""
        if not self.enabled:
            return
            
        # 休市暂停检查 (Error Backoff)
        now = time.time()
        if now < self.pause_until:
            return

        # 获取一次 tick，后续复用
        if self.lock:
            with self.lock:
                tick = mt5.symbol_info_tick(self.symbol)
        else:
            tick = mt5.symbol_info_tick(self.symbol)
            
        if not tick or tick.bid <= 0: 
            self.pause_until = now + 5
            return

        # 市场活跃度检查 (Proactive Check)
        if not self._is_market_open(tick):
            return

        # 极端点差闸门 (Fuse)
        if self.max_spread_points is not None:
            spread = tick.ask - tick.bid
            if spread > self.max_spread_points * self.point:
                Logger.log(self.symbol, "FUSE", f"spread={spread/self.point:.1f} > {self.max_spread_points}pt, cooldown {self.extreme_cooldown}s")
                self.pause_until = now + self.extreme_cooldown
                return

        # --- ATR 自适应步长逻辑 ---
        if self.use_atr:
            atr = self._calculate_atr()
            if atr:
                # 动态调整步长
                new_step = round(atr * self.atr_factor, 5)
                
                # 限制步长变化幅度，避免频繁修改
                if abs(new_step - self.step) / self.step > self.atr_change_threshold:
                    # 限制步长范围
                    min_s = self.base_step * self.min_step_mult
                    max_s = self.base_step * self.max_step_mult
                    self.step = max(min_s, min(max_s, new_step))
        
        # 价格基准：使用中间价
        mid_price = (tick.bid + tick.ask) / 2
        
        # 边界检查
        if mid_price < self.min_price or mid_price > self.max_price:
            if self.out_of_range_action == "stop":
                Logger.log(self.symbol, "STOP", f"mid {mid_price} out of range [{self.min_price}, {self.max_price}]")
                self.enabled = False
                self.clear_old_orders()
                return
            elif self.out_of_range_action == "freeze":
                # FIXED: freeze means do nothing (no trim/no add)
                # Logger.log(self.symbol, "FREEZE", f"mid {mid_price} out of range, skip maintain")
                return
            else:
                # ignore mode
                pass

        # 1. 获取当前属于本实例的挂单和持仓
        if orders_list is not None:
            orders = orders_list
            # 过滤属于本策略的订单 (增加 symbol 过滤)
            my_orders = [o for o in orders if o.magic == self.magic and o.symbol == self.symbol]
        else:
            if self.lock:
                with self.lock:
                    orders = mt5.orders_get(symbol=self.symbol)
            else:
                orders = mt5.orders_get(symbol=self.symbol)
            my_orders = [o for o in orders if o.magic == self.magic] if orders else []
        
        # 1.5 获取持仓
        if positions_list is not None:
            positions = positions_list
            # 过滤属于本策略的持仓 (增加 symbol 过滤)
            my_positions = [p for p in positions if p.symbol == self.symbol and p.magic == self.magic]
        else:
            if self.lock:
                with self.lock:
                    positions = mt5.positions_get(symbol=self.symbol)
            else:
                positions = mt5.positions_get(symbol=self.symbol)
            # 增加 magic 过滤
            my_positions = [p for p in positions if p.symbol == self.symbol and p.magic == self.magic] if positions else []
            
        # --- 状态播报 (每分钟一次) ---
        if time.time() - self._last_status_log_time > self._status_log_interval:
            float_profit = sum(p.profit for p in my_positions)
            pos_vol = sum(p.volume for p in my_positions)
            buy_orders = len([o for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT])
            sell_orders = len([o for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT])
            
            status_msg = (f"价格: {tick.bid:.{self.digits}f}/{tick.ask:.{self.digits}f} | "
                          f"持仓: {len(my_positions)}单({pos_vol}手, 浮盈{float_profit:.2f}) | "
                          f"挂单: 买{buy_orders}/卖{sell_orders} | "
                          f"Step: {self.step}")
            Logger.log(self.symbol, "STATUS", status_msg)
            self._last_status_log_time = time.time()

        # --- Anchor 初始化与再中心化 ---
        self._init_anchor_if_needed(mid_price)
        self._maybe_recenter(mid_price)

        # ========== HEDGE MANAGER (strict) ==========
        if self.hedge_enabled and self.mode == "long" and self.max_net_vol is not None:
            # 只统计本策略仓位（你务必保证 my_positions 已经按 magic+symbol 过滤）
            long_pos = [p for p in my_positions if p.type == mt5.POSITION_TYPE_BUY]
            short_pos = [p for p in my_positions if p.type == mt5.POSITION_TYPE_SELL]  # 对冲空仓

            long_vol = sum(p.volume for p in long_pos)
            short_vol = sum(p.volume for p in short_pos)
            net_vol = long_vol - short_vol

            cap = float(self.max_net_vol)
            hedge_target = cap * self.hedge_fraction           # 最多对冲 1/3 cap
            tranche = hedge_target / max(1, self.hedge_tranches)

            # gross限制（必须）
            gross = long_vol + short_vol
            if self.max_gross_vol is not None and (gross >= self.max_gross_vol):
                # 总仓位已经到顶：不再加对冲
                pass

            now = time.time()
            mid = (tick.bid + tick.ask) / 2.0

            # --- (A) 对冲盈利后 BE：先做，降低“错误对冲”的损失 ---
            be_trigger = self.be_trigger_steps * self.step
            for pos in short_pos:
                # 空单盈利：open - ask >= be_trigger
                if (pos.price_open - tick.ask) >= be_trigger:
                    self._move_sell_sl_to_breakeven(pos)

            # --- (B) 入场门槛：波动率90分位 + 成交量>=3倍 ---
            rates = self._get_m1_rates_cached(n=450, cache_sec=10)
            vol_ok, vol_cur, vol_thr = (False, None, None)
            volm_ok, v_cur, v_base = (False, None, None)
            if rates is not None:
                vol_ok, vol_cur, vol_thr = self._volatility_gate(rates)
                volm_ok, v_cur, v_base = self._volume_gate(rates)

            # 只有在“真正爆发”时才允许对冲进入
            gate_ok = (vol_ok and volm_ok)

            # --- (C) 分三次加对冲：只有 net>=cap 才开始 ---
            if net_vol >= cap and short_vol < hedge_target:
                if gate_ok and (now - self._last_hedge_time >= self.hedge_cooldown):
                    # 分段条件：继续向不利方向推进才加下一段（避免震荡误触发）
                    ok_move = (
                        self._last_hedge_entry_price is None or
                        mid <= self._last_hedge_entry_price - self.hedge_entry_steps * self.step
                    )
                    if ok_move:
                        # 还可以加多少
                        vol_to_add = min(tranche, hedge_target - short_vol)

                        # gross限制 + 本次加仓检查
                        if self.max_gross_vol is None or (gross + vol_to_add <= self.max_gross_vol):
                            res = self._open_hedge_sell(vol_to_add)
                            if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                                self._last_hedge_time = now
                                self._last_hedge_entry_price = mid
                                Logger.log(self.symbol, "HEDGE_ADD",
                                           f"add={vol_to_add:.2f} short={short_vol:.2f}/{hedge_target:.2f} "
                                           f"net={net_vol:.2f}/{cap:.2f} vol={vol_cur:.3f}>={vol_thr:.3f} "
                                           f"tv={v_cur:.1f}>={self.hedge_vol_mult}*{v_base:.1f}")

            # --- (D) 反弹/回安全区：分段退出一段 ---
            safe_net = cap * (1.0 - self.hedge_fraction)  # 例如 cap=1.5 => 1.0
            rebound = False
            if self._last_hedge_entry_price is not None:
                rebound = mid >= self._last_hedge_entry_price + self.hedge_exit_steps * self.step

            if short_vol > 0 and (now - self._last_hedge_time >= self.hedge_cooldown):
                if rebound or net_vol <= safe_net:
                    # 选一个空仓平掉一段（FIFO：ticket最小先平）
                    short_sorted = sorted(short_pos, key=lambda p: p.ticket)
                    pos = short_sorted[0]
                    vol_to_close = min(tranche, pos.volume)
                    res = self._close_sell_position(pos.ticket, vol_to_close)
                    if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                        self._last_hedge_time = now
                        Logger.log(self.symbol, "HEDGE_EXIT",
                                   f"close={vol_to_close:.2f} net={net_vol:.2f} safe={safe_net:.2f} rebound={rebound}")
        # ========== END HEDGE MANAGER ==========

        # 2. 生成目标网格层级 (围绕 Anchor 固定生成)
        target_buys = []
        target_sells = []
        
        # 搜索范围需要覆盖: window + recenter_steps + 缓冲
        # 防止因为 anchor 偏离导致生成的层级被过滤掉后数量不足
        search_range_buy = self.buy_window + self.recenter_steps + 5
        search_range_sell = self.sell_window + self.recenter_steps + 5

        # 只有在价格范围内才补单 (虽然前面有边界检查，但这里是生成逻辑)
        if self.min_price <= mid_price <= self.max_price:
            # 生成买单目标 (下方)
            if self.mode in ["neutral", "long"]:
                # 从 0 开始，围绕 Anchor 向下铺设 (包含 Anchor 本身)
                for i in range(0, search_range_buy):
                    level = self._normalize_price(self.anchor - (i * self.step))
                    if level < tick.ask and level >= self.min_price:
                        target_buys.append(level)
                # 截取窗口大小
                target_buys = target_buys[:self.buy_window]

            # 生成卖单目标 (上方)
            if self.mode in ["neutral", "short"]:
                # 从 0 开始，围绕 Anchor 向上铺设 (包含 Anchor 本身)
                for i in range(0, search_range_sell):
                    level = self._normalize_price(self.anchor + (i * self.step))
                    if level > tick.bid and level <= self.max_price:
                        target_sells.append(level)
                # 截取窗口大小
                target_sells = target_sells[:self.sell_window]
                # 按照价格从高到低排序 (符合用户习惯)
                target_sells.sort(reverse=True)

        # 3. 挂单维护逻辑
        
        # A. TRIM (清理多余/超界挂单)
        target_set = set(target_buys + target_sells)
        for o in list(my_orders):
            if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                op = self._normalize_price(o.price_open)
                should_remove = False
                
                if op not in target_set:
                    should_remove = True
                
                # 模式过滤
                if o.type == mt5.ORDER_TYPE_BUY_LIMIT and self.mode == "short":
                    should_remove = True
                if o.type == mt5.ORDER_TYPE_SELL_LIMIT and self.mode == "long":
                    should_remove = True

                if should_remove:
                    if self.lock:
                        with self.lock:
                            mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    else:
                        mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    # Logger.log(self.symbol, "TRIM", f"撤单: {op}")

        # B. 补单 (带库存风控)
        
        # 统计库存
        long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(my_positions, my_orders)

        existing_buy_prices = {self._normalize_price(o.price_open) for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT}
        existing_sell_prices = {self._normalize_price(o.price_open) for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT}
        existing_positions_prices = {self._normalize_price(p.price_open) for p in my_positions}

        min_dist = max(self.stop_level, self.point * 10) # 最小挂单距离
        placed_count = 0
        
        # 补买单
        for price in target_buys:
            if placed_count >= self.max_new_orders_per_update: break
            if price in existing_buy_prices: continue
            if abs(price - tick.ask) < min_dist: continue
            
            # 检查持仓重叠 (简单去重)
            is_duplicate_pos = False
            for p_price in existing_positions_prices:
                if abs(p_price - price) < (self.step * 0.1):
                    is_duplicate_pos = True
                    break
            if is_duplicate_pos: continue

            # 风控检查
            if not self._allow_side("buy", long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol):
                break

            if self._place_buy_order(price):
                placed_count += 1
                # 本地更新 net_vol 以便循环内即时生效
                net_vol += self.lot

        # 补卖单
        for price in target_sells:
            if placed_count >= self.max_new_orders_per_update: break
            if price in existing_sell_prices: continue
            if abs(price - tick.bid) < min_dist: continue

            is_duplicate_pos = False
            for p_price in existing_positions_prices:
                if abs(p_price - price) < (self.step * 0.1):
                    is_duplicate_pos = True
                    break
            if is_duplicate_pos: continue

            if not self._allow_side("sell", long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol):
                break

            if self._place_sell_order(price):
                placed_count += 1
                net_vol -= self.lot
