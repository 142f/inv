# strategy_lib.py
import MetaTrader5 as mt5
import time
import numpy as np
from .logger import Logger

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
        """计算 ATR (简单移动平均算法) - 向量化优化"""
        # 获取足够的数据: period + 1 根 K 线 (M15 周期)
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M15, 0, self.atr_period + 1)
        if rates is None or len(rates) < self.atr_period + 1:
            return None
            
        # 使用 numpy 向量化计算 (mt5 返回的是 numpy 结构化数组)
        high = rates['high'][1:]
        low = rates['low'][1:]
        close_prev = rates['close'][:-1]
        
        tr = np.maximum(high - low, np.abs(high - close_prev))
        tr = np.maximum(tr, np.abs(low - close_prev))
        
        return np.mean(tr)

    def _is_market_open(self, tick=None):
        """检查市场是否开放 (基于 Tick 时间)"""
        if tick is None:
            tick = mt5.symbol_info_tick(self.symbol)
        if not tick: return False
        # 如果最后一次 Tick 距离现在超过 10 分钟 (600秒)，认为休市
        if abs(time.time() - tick.time) > 600:
            return False
        return True

    def _place_buy_order(self, price):
        """内部方法：发送带止盈的买单"""
        try:
            # 重新获取最新 tick，减少时间差
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick: return None
            
            symbol_info = mt5.symbol_info(self.symbol)
            if not symbol_info:
                Logger.log(self.symbol, "ERROR", f"无法获取 {self.symbol} 信息")
                return None
            
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
                "deviation": 20,  # 允许 20 点的滑点
                "magic": self.magic,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            
            result = mt5.order_send(request)
            
            # 填充模式兼容
            if result.retcode == 10030: 
                del request["type_filling"]
                result = mt5.order_send(request)
            
            # 统一错误处理
            if result.retcode not in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                # 如果是价格变动 (Requote)，尝试重试一次
                if result.retcode == 10004: # REQUOTE
                    Logger.log(self.symbol, "WARN", "价格变动，正在重试...")
                    time.sleep(0.1)
                    # 重新获取价格并重试
                    tick = mt5.symbol_info_tick(self.symbol)
                    if tick:
                        result = mt5.order_send(request)
                        if result.retcode in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                            Logger.log(self.symbol, "ORDER_SENT", f"开仓价: {price:<10.2f} | 止盈价: {tp:<10.2f} | Magic: {self.magic} (重试)")
                            return result.order

                self._handle_order_error(result.retcode, result.comment, price)
                return None
                
            Logger.log(self.symbol, "ORDER_SENT", f"开仓价: {price:<10.2f} | 止盈价: {tp:<10.2f} | Magic: {self.magic}")
            return result.order
            
        except Exception as e:
            Logger.log(self.symbol, "EXCEPTION", f"下单异常: {str(e)}")
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
        orders = mt5.orders_get(symbol=self.symbol)
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    if res.retcode == 10018: # MARKET_CLOSED
                        Logger.log(self.symbol, "WARN", "市场休市，无法撤单，暂停运行 5 分钟")
                        self.pause_until = time.time() + 300
                        return
            Logger.log(self.symbol, "CLEANUP", "历史挂单已清理")

    def update(self, orders_list=None, positions_list=None):
        """核心巡检逻辑：改为接收外部注入的数据"""
        if not self.enabled:
            return
            
        # 休市暂停检查 (Error Backoff)
        if time.time() < self.pause_until:
            return

        # 获取一次 tick 和 symbol_info，后续复用
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick or tick.bid <= 0: return

        symbol_info = mt5.symbol_info(self.symbol)
        if not symbol_info: return
        digits = symbol_info.digits

        # 市场活跃度检查 (Proactive Check)
        if not self._is_market_open(tick):
            return

        # --- ATR 自适应步长逻辑 ---
        if self.use_atr:
            atr = self._calculate_atr()
            if atr:
                # 动态调整步长，但保留最小值防止过小 (例如不小于 base_step 的 0.5 倍)
                new_step = round(atr * self.atr_factor, 5)
                # 限制步长范围，防止过大或过小
                self.step = max(new_step, self.base_step * 0.5)
        
        curr_price = tick.bid
        # 边界检查：如果现价不在设定的总范围内，不进行操作
        if curr_price < self.min_price or curr_price > self.max_price:
            return

        # 计算基准网格线 (最近的整数网格)
        if self.step <= 0:
            Logger.log(self.symbol, "ERROR", f"Step 异常: {self.step}")
            return

        base_level = round(curr_price / self.step) * self.step
        
        # 1. 获取当前属于本实例的挂单和持仓 (优化：使用集合)
        # 使用注入的数据，如果未传入（如单体测试时）则回退到原逻辑
        if orders_list is not None:
            orders = orders_list
            # 既然是注入的，说明已经按 magic 分组了，无需再次检查 magic
            existing_prices = {round(o.price_open, digits) for o in orders}
        else:
            orders = mt5.orders_get(symbol=self.symbol)
            existing_prices = {round(o.price_open, digits) for o in orders if o.magic == self.magic} if orders else set()
        
        if positions_list is not None:
            positions = positions_list
            existing_positions = {round(p.price_open, digits) for p in positions}
        else:
            positions = mt5.positions_get(symbol=self.symbol)
            existing_positions = {round(p.price_open, digits) for p in positions if p.magic == self.magic} if positions else set()

        # 2. 计算目标位 (智能滑动窗口)
        # 扩大搜索范围，确保能找到最近的 window 个网格
        target_levels = []
        for i in range(-self.window - 2, 5):
            level = round(base_level + (i * self.step), digits)
            # 必须低于现价 (Buy Limit)，且在策略设定的 min_price 之上
            if level < curr_price and level >= self.min_price:
                target_levels.append(level)
        
        # 排序并只取离现价最近的 window 个 (从大到小)
        target_levels.sort(reverse=True)
        target_levels = target_levels[:self.window]

        # 新增：挂单数量限制检查 - 当挂单超过窗口限制时，取消最远的挂单
        # 确保 orders 是列表以便修改
        if orders and not isinstance(orders, list):
            orders = list(orders)
            
        if orders:
            # 筛选出属于本策略的挂单
            my_orders = [o for o in orders if o.magic == self.magic]
            
            if len(my_orders) > self.window:
                # 按距离当前价格排序，取消最远的挂单
                orders_by_distance = sorted(my_orders, key=lambda o: abs(o.price_open - curr_price), reverse=True)
                
                # 计算需要取消的数量
                to_remove_count = len(my_orders) - self.window
                
                for i in range(to_remove_count):
                    o = orders_by_distance[i]
                    Logger.log(self.symbol, "WINDOW_LIMIT", f"数量超限: {len(my_orders)}/{self.window} | 取消挂单: {o.price_open:<10}")
                    res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    
                    if res.retcode == mt5.TRADE_RETCODE_DONE:
                        if o in orders:
                            orders.remove(o)  # 从主列表中移除
                    else:
                        Logger.log(self.symbol, "ERROR", f"取消挂单失败: {res.comment} ({res.retcode})")
                        # 失败时暂停一下
                        self.pause_until = time.time() + 2

        # 3. 补单逻辑
        # symbol_info 已在函数开头获取

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
                Logger.log(self.symbol, "FILL_GRID", f"目标价: {level:<10.2f} | 当前价: {curr_price:<10.2f}")
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
                        Logger.log(self.symbol, "RM_FAR", f"挂单价: {o.price_open:<10.2f} | 距离值: {dist:<10.2f}")
                        res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                        if res.retcode != mt5.TRADE_RETCODE_DONE:
                            Logger.log(self.symbol, "ERROR", f"删除失败: {res.comment} ({res.retcode})")
                            # 删除失败也暂停一下，防止死循环尝试删除
                            self.pause_until = time.time() + 5
