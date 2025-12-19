import MetaTrader5 as mt5
import time
import os
from dotenv import load_dotenv # 导入加载库

# ==========================================
# 1. 核心配置区域 (从环境变量读取)
# ==========================================
# 加载当前目录下的 .env 文件
load_dotenv()

# 使用 os.getenv 读取，若读取不到可设置默认值
# 注意：这里为了演示方便，如果读取不到会报错或使用默认值，实际使用请确保 .env 配置正确
env_account_id = os.getenv("MT5_ACCOUNT_ID")
ACCOUNT_ID = int(env_account_id) if env_account_id else 0
PASSWORD   = os.getenv("MT5_PASSWORD", "")
SERVER     = os.getenv("MT5_SERVER", "Exness-MT5Real23")
SYMBOL     = os.getenv("MT5_SYMBOL", "BTCUSDc")
MT5_PATH   = os.getenv("MT5_PATH") # 新增：支持自定义 MT5 路径

# 策略参数 (通常不敏感，可以直接保留或同样放入 .env)
STEP         = 200.0            # 网格间距 (美元)
TP_DISTANCE  = 200.0            # 止盈距离
LOT          = 0.05             # 下单手数
MAGIC_NUMBER = 20251218        
WINDOW_SIZE  = 10               # 保持现价下方始终有 10 层挂单

def initialize_mt5():
    """初始化并登录账户"""
    # 如果配置了路径，则指定路径初始化
    init_params = {"path": MT5_PATH} if MT5_PATH else {}
    
    if not mt5.initialize(**init_params):
        print(f"MT5 初始化失败, 错误代码: {mt5.last_error()}")
        if MT5_PATH:
            print(f"尝试使用的路径: {MT5_PATH}")
        else:
            print("提示: 未配置 MT5_PATH，尝试使用默认路径失败。请在 .env 中配置 MT5_PATH。")
        return False
    # 使用从环境变量获取的密码登录
    if not mt5.login(ACCOUNT_ID, password=PASSWORD, server=SERVER):
        print(f"登录失败: {mt5.last_error()}")
        return False
    
    if not mt5.symbol_select(SYMBOL, True):
        print(f"致命错误：找不到品种 {SYMBOL}")
        return False

    acc = mt5.account_info()
    if acc:
        print(f"--- 系统就绪 ---")
        print(f"账户: {acc.login} | 余额: {acc.balance} | 模式: {'Netting' if acc.margin_mode == 0 else 'Hedging'}")
        return True
    else:
        print("无法获取账户信息")
        return False

def place_buy_order(price):
    """下单函数"""
    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": SYMBOL,
        "volume": LOT,
        "type": mt5.ORDER_TYPE_BUY_LIMIT,
        "price": price,
        "sl": 0.0,
        "tp": price + TP_DISTANCE,
        "magic": MAGIC_NUMBER,
        "comment": "Grid Order",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"下单失败: {result.comment}")
    else:
        print(f"成功下单: {price}")

def clear_grid_orders():
    """清理所有网格挂单"""
    orders = mt5.orders_get(symbol=SYMBOL)
    if orders:
        for o in orders:
            if o.magic == MAGIC_NUMBER:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
        print("已清理旧挂单")

def manage_long_grid():
    """核心逻辑：动态浮动网格"""
    tick = mt5.symbol_info_tick(SYMBOL)
    
    if not tick or tick.bid <= 0: 
        return
    
    curr_price = tick.bid
    base_level = round(curr_price / STEP) * STEP
    
    orders = mt5.orders_get(symbol=SYMBOL)
    existing_prices = []
    if orders:
        existing_prices = [round(o.price_open, 2) for o in orders if o.magic == MAGIC_NUMBER]

    target_levels = []
    # 采用你提到的修改后的循环，确保包含 88200 等临界点位
    for i in range(-WINDOW_SIZE + 1, 1): # i 取值从 -9 到 0
        level = round(base_level + (i * STEP), 2)
        if level < curr_price:
            target_levels.append(level)

    # 3. 动态补单
    for level in target_levels:
        if level not in existing_prices:
            if abs(level - curr_price) < (STEP * 0.3): continue
            
            print(f"[{time.strftime('%H:%M:%S')}] 动态补全买单: {level}")
            # 调用下单函数
            place_buy_order(level)

    # 4. 动态清理
    if orders:
        for o in orders:
            if o.magic == MAGIC_NUMBER:
                price_rounded = round(o.price_open, 2)
                if price_rounded not in target_levels or o.price_open > curr_price:
                    print(f"价格移出窗口，清理冗余挂单: {o.price_open}")
                    mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})

if __name__ == "__main__":
    if initialize_mt5():
        # ... 剩余主循环逻辑保持不变 ...
        clear_grid_orders() 
        print(">>> 动态滑动网格已启动（环境变量加载成功）")
        try:
            while True:
                manage_long_grid()
                time.sleep(1) 
        except KeyboardInterrupt:
            print("脚本停止。")
    mt5.shutdown()
