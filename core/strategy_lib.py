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
                 lock=None):
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
            '_last_atr_time': self._last_atr_time
        }

    def set_state(self, state):
        """恢复策略内部状态"""
        if state:
            self.pause_until = state.get('pause_until', self.pause_until)
            self.enabled = state.get('enabled', self.enabled)
            self._last_atr_value = state.get('_last_atr_value', self._last_atr_value)
            self._last_tick_time = state.get('_last_tick_time', self._last_tick_time)
            self._last_atr_time = state.get('_last_atr_time', self._last_atr_time)

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
        return round(price, self.digits)

    def _normalize_volume(self, vol):
        # 简单的步长取整
        if self.vol_step > 0:
            steps = round(vol / self.vol_step)
            vol = steps * self.vol_step
        return round(max(self.vol_min, min(self.vol_max, vol)), 2)

    def _get_grid_level(self, price):
        """[优化] 锚定网格计算"""
        if self.step <= 0: return price
        # 默认锚点为 0，即绝对网格
        return round(price / self.step) * self.step

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
                Logger.log(self.symbol, "ERROR", "下单请求返回 None (可能是连接断开)")
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
                    Logger.log(self.symbol, "ERROR", "下单请求返回 None (重试时)")
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
                            Logger.log(self.symbol, "ERROR", "下单请求返回 None (Requote重试时)")
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
                Logger.log(self.symbol, "ERROR", "下单请求返回 None (可能是连接断开)")
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
                    Logger.log(self.symbol, "ERROR", "下单请求返回 None (重试时)")
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
                        Logger.log(self.symbol, "ERROR", "下单请求返回 None (Requote重试时)")
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

    def update(self, orders_list=None, positions_list=None):
        """核心巡检逻辑：支持双向网格与对标交易所模式"""
        if not self.enabled:
            return
            
        # 休市暂停检查 (Error Backoff)
        if time.time() < self.pause_until:
            return

        # 获取一次 tick，后续复用
        if self.lock:
            with self.lock:
                tick = mt5.symbol_info_tick(self.symbol)
        else:
            tick = mt5.symbol_info_tick(self.symbol)
            
        if not tick or tick.bid <= 0: return

        # 市场活跃度检查 (Proactive Check)
        if not self._is_market_open(tick):
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
        
        existing_buy_prices = {self._normalize_price(o.price_open) for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT}
        existing_sell_prices = {self._normalize_price(o.price_open) for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT}

        # 1.5 获取持仓
        if positions_list is not None:
            positions = positions_list
            # 过滤属于本策略的持仓 (增加 symbol 过滤)
            my_positions = [p for p in positions if p.symbol == self.symbol]
            existing_positions = {self._normalize_price(p.price_open) for p in my_positions}
        else:
            if self.lock:
                with self.lock:
                    positions = mt5.positions_get(symbol=self.symbol)
            else:
                positions = mt5.positions_get(symbol=self.symbol)
            existing_positions = {self._normalize_price(p.price_open) for p in positions if p.magic == self.magic} if positions else set()

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

        # 边界检查
        if mid_price < self.min_price or mid_price > self.max_price:
            if self.out_of_range_action == "stop":
                Logger.log(self.symbol, "STOP", f"价格 {mid_price} 超出范围 [{self.min_price}, {self.max_price}]，停止策略")
                self.enabled = False
                self.clear_old_orders()
                return
            elif self.out_of_range_action == "freeze":
                # 仅清理明显超界的挂单，不补单
                pass
            else:
                return

        # 计算基准网格线
        base_level = self._get_grid_level(mid_price)

        # 2. 生成目标网格层级
        target_buys = []
        target_sells = []
        
        # 只有在价格范围内才补单
        if self.min_price <= mid_price <= self.max_price:
            # 生成买单目标 (下方)
            if self.mode in ["neutral", "long"]:
                for i in range(1, self.buy_window + 2):
                    level = self._normalize_price(base_level - (i * self.step))
                    # 买单必须低于 Ask (防止立刻成交)
                    if level < tick.ask and level >= self.min_price:
                        target_buys.append(level)
                target_buys = sorted(target_buys, reverse=True)[:self.buy_window]

            # 生成卖单目标 (上方)
            if self.mode in ["neutral", "short"]:
                for i in range(1, self.sell_window + 2):
                    level = self._normalize_price(base_level + (i * self.step))
                    # 卖单必须高于 Bid
                    if level > tick.bid and level <= self.max_price:
                        target_sells.append(level)
                target_sells = sorted(target_sells)[:self.sell_window]

        # 3. 挂单维护逻辑
        
        # A. 清理多余/超界挂单
        for o in my_orders:
            p = self._normalize_price(o.price_open)
            should_remove = False
            
            if o.type == mt5.ORDER_TYPE_BUY_LIMIT:
                if self.mode == "short": should_remove = True
                elif p not in target_buys:
                    should_remove = True
            
            elif o.type == mt5.ORDER_TYPE_SELL_LIMIT:
                if self.mode == "long": should_remove = True
                elif p not in target_sells:
                    should_remove = True
            
            if should_remove:
                if self.lock:
                    with self.lock:
                        mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                else:
                    mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                Logger.log(self.symbol, "TRIM", f"撤单: {p} (不在目标网格中)")

        # B. 补单
        # 补买单
        for price in target_buys:
            if price in existing_buy_prices: continue
            
            # 检查持仓
            has_pos = False
            for p_price in existing_positions:
                if abs(p_price - price) < (self.step * 0.1):
                    has_pos = True
                    break
            if has_pos: 
                # Logger.log(self.symbol, "SKIP", f"价格 {price} 已有持仓，跳过补单")
                continue
            
            self._place_buy_order(price)
                
        # 补卖单
        for price in target_sells:
            if price in existing_sell_prices: continue
            
            # 检查持仓
            has_pos = False
            for p_price in existing_positions:
                if abs(p_price - price) < (self.step * 0.1):
                    has_pos = True
                    break
            if has_pos: 
                # Logger.log(self.symbol, "SKIP", f"价格 {price} 已有持仓，跳过补单")
                continue
            
            self._place_sell_order(price)
