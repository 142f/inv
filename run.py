# run.py
import MetaTrader5 as mt5
import time
import os
import yaml
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from core.strategy_lib import GridStrategy
from core.logger import Logger
from core.security import Security

load_dotenv()

# 使用绝对路径确保在任何目录下运行都能找到配置文件
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config", "strategies.yaml")
last_config_mtime = 0
active_strategies = {} # 使用字典管理实例: {magic: strategy_instance}
security = Security() # 初始化安全模块

def load_configs():
    """从 YAML 加载配置清单"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        Logger.log("SYSTEM", "ERROR", f"读取配置失败: {e}")
        return []

def validate_strategy_config(cfg):
    """验证策略配置的有效性"""
    required_fields = ['symbol', 'step', 'tp_dist', 'lot', 'magic']
    
    # 检查必填字段
    for field in required_fields:
        if field not in cfg:
            raise ValueError(f"策略配置缺少必要字段: {field}")
    
    # 验证数值范围
    if cfg.get('step', 0) <= 0:
        raise ValueError("网格间距必须大于0")
    
    if cfg.get('lot', 0) <= 0:
        raise ValueError("手数必须大于0")
    
    # 验证价格范围
    min_p = cfg.get('min_p', 0)
    max_p = cfg.get('max_p', 999999)
    if min_p >= max_p:
        raise ValueError(f"min_p({min_p}) 必须小于 max_p({max_p})")
    
    return True

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
            try:
                validate_strategy_config(cfg)
            except ValueError as e:
                Logger.log("SYSTEM", "CONFIG_ERROR", f"配置验证失败: {e}")
                continue

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
                s.use_atr = cfg.get('use_atr', s.use_atr)
                s.atr_period = cfg.get('atr_period', s.atr_period)
                s.atr_factor = cfg.get('atr_factor', s.atr_factor)
                
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
    pwd = os.getenv("MT5_PASSWORD")
    srv = os.getenv("MT5_SERVER")
    mt5_path = os.getenv("MT5_PATH")
    
    # 尝试解密
    if acc_id_str and acc_id_str.startswith("gAAAA"):
        decrypted_acc = security.decrypt(acc_id_str)
        if decrypted_acc: acc_id_str = decrypted_acc
        
    if pwd and pwd.startswith("gAAAA"):
        decrypted_pwd = security.decrypt(pwd)
        if decrypted_pwd: pwd = decrypted_pwd
            
    if srv and srv.startswith("gAAAA"):
        decrypted_srv = security.decrypt(srv)
        if decrypted_srv: srv = decrypted_srv

    acc_id = int(acc_id_str) if acc_id_str and acc_id_str.isdigit() else 0

    # 初始化参数
    init_params = {}
    if mt5_path:
        init_params["path"] = mt5_path.strip().strip('"').strip("'").strip()

    # 尝试初始化
    if not mt5.initialize(**init_params) and not mt5.initialize():
        Logger.log("SYSTEM", "ERROR", f"MT5 Init Failed: {mt5.last_error()}")
        return False

    # --- 智能登录逻辑 ---
    # 1. 检查当前终端是否已经登录了正确的账号
    current_account_info = mt5.account_info()
    if current_account_info and current_account_info.login == acc_id:
        Logger.log("SYSTEM", "INFO", f"检测到终端已登录账号 {acc_id}，跳过重复登录")
        return True

    # 2. 如果未登录或账号不一致，则尝试登录
    Logger.log("SYSTEM", "INFO", f"正在尝试登录账号 {acc_id}...")
    if not mt5.login(acc_id, password=pwd, server=srv):
        Logger.log("SYSTEM", "ERROR", f"Login Failed: {mt5.last_error()} (请检查 .env 中的账号/密码/服务器)")
        return False
    return True

if __name__ == "__main__":
    if initialize_system():
        Logger.log("SYSTEM", "START", "热加载网格系统已就绪")
        # 首次加载
        sync_strategies()
        
        # 创建全局线程池
        executor = ThreadPoolExecutor(max_workers=4)

        try:
            while True:
                # 全局熔断检查 (Circuit Breaker)
                acc = mt5.account_info()
                if acc and acc.margin_level < 200 and acc.margin_level > 0:
                     Logger.log("SYSTEM", "HALT", f"保证金过低 ({acc.margin_level}%)，暂停运行")
                     time.sleep(5)
                     continue

                # 每次循环开始前检查配置是否更新
                sync_strategies()
                
                # --- 新增：批处理 I/O ---
                # 一次性拿回所有挂单和持仓
                all_orders = mt5.orders_get()
                all_positions = mt5.positions_get()

                # 根据 magic 分组，避免在策略内部循环查找，复杂度由 O(N^2) 降为 O(N)
                orders_by_magic = defaultdict(list)
                if all_orders:
                    for o in all_orders:
                        orders_by_magic[o.magic].append(o)

                positions_by_magic = defaultdict(list)
                if all_positions:
                    for p in all_positions:
                        positions_by_magic[p.magic].append(p)

                # 执行所有活跃策略的巡检 (并发版)
                futures = []
                for magic, s in active_strategies.items():
                    # 并发执行逻辑
                    f = executor.submit(
                        s.update, 
                        orders_list=orders_by_magic[magic], 
                        positions_list=positions_by_magic[magic]
                    )
                    futures.append(f)

                # 等待本轮所有策略处理完毕，确保下一轮循环开始前数据是同步的
                for f in futures: f.result()
                
                time.sleep(0.2) # 提高频率到 200ms
        except KeyboardInterrupt:
            Logger.log("SYSTEM", "STOP", "手动停止")
    mt5.shutdown()
