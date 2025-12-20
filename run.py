# run.py
import MetaTrader5 as mt5
import time
import os
import json
from dotenv import load_dotenv
from strategy_lib import GridStrategy
from logger import Logger

load_dotenv()

CONFIG_FILE = "strategies.json"
last_config_mtime = 0
active_strategies = {} # 使用字典管理实例: {magic: strategy_instance}

def load_configs():
    """从 JSON 加载配置清单"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        Logger.log("SYSTEM", "ERROR", f"读取配置失败: {e}")
        return []

def sync_strategies():
    """同步策略实例：热加载核心"""
    global last_config_mtime
    try:
        current_mtime = os.path.getmtime(CONFIG_FILE)
    except FileNotFoundError:
        return

    if current_mtime > last_config_mtime:
        Logger.log("SYSTEM", "RELOAD", "检测到配置变更，正在同步策略...")
        new_configs = load_configs()
        if not new_configs: return # 如果读取失败或为空，不进行更新

        new_magics = [cfg['magic'] for cfg in new_configs]
        
        # 1. 更新或新增策略
        for cfg in new_configs:
            m = cfg['magic']
            if m not in active_strategies:
                Logger.log("SYSTEM", "ADD", f"增加新策略: {cfg['symbol']} (Magic: {m})")
                strategy = GridStrategy(**cfg)
                active_strategies[m] = strategy
                mt5.symbol_select(cfg['symbol'], True)
                # 启动时清理旧挂单
                strategy.clear_old_orders()
            else:
                # 更新已有策略的开关状态和其他参数
                s = active_strategies[m]
                s.enabled = cfg.get('enabled', True)
                s.step = cfg.get('step', s.step)
                s.tp_dist = cfg.get('tp_dist', s.tp_dist)
                s.lot = cfg.get('lot', s.lot)
                s.window = cfg.get('window', s.window)
                s.min_price = cfg.get('min_p', s.min_price)
                s.max_price = cfg.get('max_p', s.max_price)
                
                Logger.log("SYSTEM", "UPDATE", f"已同步策略状态: {cfg['symbol']} (Enabled: {s.enabled})")
        
        # 2. 移除已删除的策略
        for m in list(active_strategies.keys()):
            if m not in new_magics:
                Logger.log("SYSTEM", "REMOVE", f"移除策略 Magic: {m}")
                del active_strategies[m]
        
        last_config_mtime = current_mtime

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
        Logger.log("SYSTEM", "START", "热加载网格系统已就绪")
        # 首次加载
        sync_strategies()
        
        try:
            while True:
                # 每次循环开始前检查配置是否更新
                sync_strategies()
                
                # 执行所有活跃策略的巡检
                for magic, s in active_strategies.items():
                    s.update()
                
                time.sleep(1)
        except KeyboardInterrupt:
            Logger.log("SYSTEM", "STOP", "手动停止")
    mt5.shutdown()
