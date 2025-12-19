import MetaTrader5 as mt5
import time
import os
from datetime import datetime
from dotenv import load_dotenv

# 加载环境变量 (读取账号密码)
load_dotenv()

# ==========================================
# 0. 日志工具类
# ==========================================
class Logger:
    @staticmethod
    def log(symbol, action, message):
        """
        统一日志格式: [时间] [品种] [动作] 消息内容
        """
        t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{t} | {symbol:<9} | {action:<10} | {message}")

# ==========================================
# 1. 网格策略类 (OOP 架构)
# ==========================================
class GridStrategy:
    def __init__(self, symbol, step, tp_dist, lot, magic, window=6, min_p=0, max_p=999999):
        """
        :param symbol: 交易品种 (如 BTCUSDc)
        :param step: 网格间距
        :param tp_dist: 止盈距离 (如 XAU 的 3 个点)
        :param lot: 下单手数
        :param magic: 脚本识别码 (必须唯一)
        :param window: 滑动窗口大小
        :param min_p: 价格运行下限 (用于范围网格)
        :param max_p: 价格运行上限 (用于范围网格)
        """
        self.symbol = symbol
        self.step = step
        self.tp_dist = tp_dist
        self.lot = lot
        self.magic = magic
        self.window = window
        self.min_price = min_p
        self.max_price = max_p

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
        
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            Logger.log(self.symbol, "ORDER_SENT", f"Price: {price} | TP: {tp} | Magic: {self.magic}")

    def clear_old_orders(self):
        """启动时清理旧网格挂单"""
        orders = mt5.orders_get(symbol=self.symbol)
        if orders:
            for o in orders:
                if o.magic == self.magic:
                    mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            Logger.log(self.symbol, "CLEANUP", "History orders cleared")

    def update(self):
        """核心巡检逻辑：每轮循环执行一次"""
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

# ==========================================
# 2. 主程序控制流
# ==========================================
def initialize_system():
    # 从 .env 读取配置
    acc_id_str = os.getenv("MT5_ACCOUNT_ID")
    acc_id = int(acc_id_str) if acc_id_str else 0
    pwd = os.getenv("MT5_PASSWORD")
    srv = os.getenv("MT5_SERVER")
    mt5_path = os.getenv("MT5_PATH")

    # 初始化参数
    init_params = {}
    if mt5_path:
        init_params["path"] = mt5_path

    if not mt5.initialize(**init_params):
        Logger.log("SYSTEM", "ERROR", "MT5 Init Failed")
        return False
    if not mt5.login(acc_id, password=pwd, server=srv):
        Logger.log("SYSTEM", "ERROR", f"Login Failed: {mt5.last_error()}")
        return False
    return True

if __name__ == "__main__":
    if initialize_system():
        # 实例化多套策略
        strategies = [
            # 策略 1: 之前的 BTCUSDc 动态滑动网格
            GridStrategy(symbol="BTCUSDc", step=200.0, tp_dist=200.0, lot=0.03, magic=20251218),
            
            # 策略 2: 新增 XAUUSDc 范围网格 (4170-4400)
            # 提示：黄金的 3 个点通常指 3.00 美元（对于 XAUUSDc 可能需要根据点位精度调整）
            GridStrategy(symbol="XAUUSDc", step=3.0, tp_dist=3.0, lot=0.02, magic=20251219, 
                         min_p=4170.0, max_p=4400.0)
        ]

        # 启动清理
        for s in strategies:
            mt5.symbol_select(s.symbol, True)
            s.clear_old_orders()

        Logger.log("SYSTEM", "START", "Multi-Strategy System Started")
        try:
            while True:
                for s in strategies:
                    s.update()
                time.sleep(1) # 高频巡检
        except KeyboardInterrupt:
            Logger.log("SYSTEM", "STOP", "System Stopped")
    mt5.shutdown()
