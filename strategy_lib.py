# strategy_lib.py
import MetaTrader5 as mt5
import time
from logger import Logger

class GridStrategy:
    def __init__(self, symbol, step, tp_dist, lot, magic, window=6, min_p=0, max_p=999999, enabled=True, use_atr=False, atr_period=14, atr_factor=1.0):
        """
        :param use_atr: 是否启用 ATR 自适应步长
        :param atr_period: ATR 计算周期 (默认 14)
        :param atr_factor: ATR 乘数 (Step = ATR * factor)
        """
        self.symbol = symbol
        self.base_step = step # 保存初始步长
        self.step = step
        self.tp_dist = tp_dist
        self.lot = lot
        self.magic = magic
        self.window = window
        self.min_price = min_p
        self.max_price = max_p
        self.enabled = enabled
        self.pause_until = 0
        self.use_atr = use_atr
        self.atr_period = atr_period
        self.atr_factor = atr_factor

    def _calculate_atr(self):
        """计算 ATR (简单移动平均算法)"""
        # 获取足够的数据: period + 1 根 K 线 (M15 周期)
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M15, 0, self.atr_period + 1)
        if rates is None or len(rates) < self.atr_period + 1:
            return None
            
        tr_sum = 0.0
        for i in range(1, len(rates)):
            high = rates[i]['high']
            low = rates[i]['low']
            close_prev = rates[i-1]['close']
            
            tr = max(high - low, abs(high - close_prev), abs(low - close_prev))
            tr_sum += tr
            
        return tr_sum / self.atr_period

    def _is_market_open(self):
        """检查市场是否开放 (基于 Tick 时间)"""
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick: return False
        # 如果最后一次 Tick 距离现在超过 10 分钟 (600秒)，认为休市
        if abs(time.time() - tick.time) > 600:
            return False
        return True

    def _place_buy_order(self, price):
        """内部方法：发送带止盈的买单"""
        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info: return
        
        digits = symbol_info.digits
        price = round(float(price), digits)
        tp = round(price + self.tp_dist, digits)

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": self.lot,
            "type": mt5.ORDER_TYPE_BUY_LIMIT,
            "price": price,
            "tp": tp,
            "magic": self.magic,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result.retcode == 10030: # 填充模式兼容
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
        
        if result.retcode == mt5.TRADE_RETCODE_DONE or result.retcode == mt5.TRADE_RETCODE_PLACED:
            Logger.log(self.symbol, "ORDER_SENT", f"Price: {price} | TP: {tp} | Magic: {self.magic}")
        elif result.retcode == 10018: # MARKET_CLOSED
            Logger.log(self.symbol, "SLEEP", "市场休市，暂停运行 5 分钟")
            self.pause_until = time.time() + 300
        else:
            Logger.log(self.symbol, "ORDER_FAIL", f"RC: {result.retcode} ({result.comment}) | Price: {price}")

    def clear_old_orders(self):
        """启动时清理旧网格挂单"""
        orders = mt5.orders_get(symbol=self.symbol)
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    if res.retcode == 10018: # MARKET_CLOSED
                        Logger.log(self.symbol, "WARN", "市场休市，无法撤单，暂停运行 5 分钟")
                        self.pause_until = time.time() + 300
                        return
            Logger.log(self.symbol, "CLEANUP", "History orders cleared")

    def update(self):
        """核心巡检逻辑：每轮循环执行一次"""
        if not self.enabled:
            return
            
        # 休市暂停检查 (Error Backoff)
        if time.time() < self.pause_until:
            return

        # 市场活跃度检查 (Proactive Check)
        if not self._is_market_open():
            return

        # --- ATR 自适应步长逻辑 ---
        if self.use_atr:
            atr = self._calculate_atr()
            if atr:
                # 动态调整步长，但保留最小值防止过小 (例如不小于 base_step 的 0.5 倍)
                new_step = round(atr * self.atr_factor, 5)
                # 限制步长范围，防止过大或过小
                self.step = max(new_step, self.base_step * 0.5)

        tick = mt5.symbol_info_tick(self.symbol)
        if not tick or tick.bid <= 0: return
        
        curr_price = tick.bid
        # 边界检查：如果现价不在设定的总范围内，不进行操作
        if curr_price < self.min_price or curr_price > self.max_price:
            return

        # 计算基准网格线 (最近的整数网格)
        base_level = round(curr_price / self.step) * self.step
        
        # 1. 获取当前属于本实例的挂单和持仓
        orders = mt5.orders_get(symbol=self.symbol)
        existing_prices = [round(o.price_open, 2) for o in orders if o.magic == self.magic] if orders else []
        
        positions = mt5.positions_get(symbol=self.symbol)
        existing_positions = [round(p.price_open, 2) for p in positions if p.magic == self.magic] if positions else []

        # 2. 计算目标位 (智能滑动窗口)
        # 扩大搜索范围，确保能找到最近的 window 个网格
        target_levels = []
        for i in range(-self.window - 2, 5):
            level = round(base_level + (i * self.step), 2)
            # 必须低于现价 (Buy Limit)，且在策略设定的 min_price 之上
            if level < curr_price and level >= self.min_price:
                target_levels.append(level)
        
        # 排序并只取离现价最近的 window 个 (从大到小)
        target_levels.sort(reverse=True)
        target_levels = target_levels[:self.window]

        # 3. 补单逻辑
        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info: return

        # 计算最小安全距离 (防止挂单太近报错，或防止在价格线上反复挂单)
        stop_level = symbol_info.trade_stops_level * symbol_info.point
        # 最小间距：取 (StopLevel + 2点) 和 (Step * 0.1) 的较大值
        min_gap = max(stop_level + 2 * symbol_info.point, self.step * 0.1)

        for level in target_levels:
            # 检查是否已有挂单
            if level in existing_prices:
                continue
                
            # 检查是否已有持仓 (防止重复开仓)
            # 由于滑点存在，持仓价格可能不完全等于 level，需要允许一定误差
            has_position = False
            for pos_price in existing_positions:
                if abs(pos_price - level) < (self.step * 0.5): # 误差范围设为间距的一半
                    has_position = True
                    break
            
            if has_position:
                continue

            # 关键逻辑：只有当 (现价 - 目标价) > 最小间距 时才补单
            # 这实现了"价格超过网格一定距离后才重新挂"的需求
            if (curr_price - level) > min_gap:
                Logger.log(self.symbol, "FILL_GRID", f"Level: {level} | Curr: {curr_price}")
                self._place_buy_order(level)

        # 4. 滑动清理逻辑 (优化版)
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    # 纯距离判断：只有当订单距离现价非常远 (超过窗口 + 3层) 时才撤销
                    # 这样即使订单暂时不在 target_levels 里 (比如快成交时)，也不会被误删
                    dist = abs(o.price_open - curr_price)
                    safe_zone = (self.window + 3) * self.step
                    
                    if dist > safe_zone:
                        Logger.log(self.symbol, "RM_FAR", f"Price: {o.price_open} | Dist: {dist:.2f}")
                        mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
