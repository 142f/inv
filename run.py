# run.py
import MetaTrader5 as mt5
import time
import os
import yaml
import argparse
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
            data = yaml.safe_load(f)
            return data if data is not None else []
    except Exception as e:
        Logger.log("SYSTEM", "ERROR", f"读取配置失败: {e}")
        return None

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
        if new_configs is None: return # 如果读取失败，不进行更新

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
                
                # 保存当前内部状态，避免状态丢失
                current_state = s.get_state()
                
                # 如果 symbol 发生变化，需要更新并订阅
                if s.symbol != cfg['symbol']:
                    s.symbol = cfg['symbol']
                    mt5.symbol_select(s.symbol, True)
                    Logger.log("SYSTEM", "UPDATE", f"策略 {m} 品种变更为: {s.symbol}")

                # 更新配置参数
                s.enabled = cfg.get('enabled', s.enabled)
                s.step = cfg.get('step', s.step)
                s.tp_dist = cfg.get('tp_dist', s.tp_dist)
                s.lot = cfg.get('lot', s.lot)
                s.window = cfg.get('window', s.window)
                s.min_price = cfg.get('min_p', s.min_price)
                s.max_price = cfg.get('max_p', s.max_price)
                s.use_atr = cfg.get('use_atr', s.use_atr)
                s.atr_period = cfg.get('atr_period', s.atr_period)
                s.atr_factor = cfg.get('atr_factor', s.atr_factor)
                
                # 恢复内部状态
                s.set_state(current_state)
                
                Logger.log("SYSTEM", "UPDATE", f"已同步策略状态: {cfg['symbol']} (Enabled: {s.enabled})")
        
        # 2. 移除已删除的策略
        for m in list(active_strategies.keys()):
            if m not in new_magics:
                Logger.log("SYSTEM", "REMOVE", f"移除策略 Magic: {m}")
                # 先清理该策略的挂单再移除
                strategy = active_strategies[m]
                strategy.clear_old_orders()
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
    if acc_id != 0 and current_account_info and current_account_info.login == acc_id:
        Logger.log("SYSTEM", "INFO", f"检测到终端已登录账号 {acc_id}，跳过重复登录")
        return True

    # 2. 如果未登录或账号不一致，则尝试登录
    if acc_id != 0:
        Logger.log("SYSTEM", "INFO", f"正在尝试登录账号 {acc_id}...")
        if not mt5.login(acc_id, password=pwd, server=srv):
            Logger.log("SYSTEM", "ERROR", f"Login Failed: {mt5.last_error()} (请检查 .env 中的账号/密码/服务器)")
            return False
    else:
        if current_account_info:
            Logger.log("SYSTEM", "WARN", f"未配置指定账号，使用当前终端账号: {current_account_info.login}")
        else:
            Logger.log("SYSTEM", "ERROR", "未配置账号且当前终端未登录")
            return False
            
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Grid trading runner (bounded loop; no infinite loops)")
    parser.add_argument(
        "--cycles",
        type=int,
        default=int(os.getenv("INV_CYCLES", "1")),
        help="How many cycles to run (default: 1).",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=float(os.getenv("INV_MAX_SECONDS", "0")),
        help="Optional max runtime in seconds; stops when exceeded (0 disables).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("INV_INTERVAL", "1.0")),
        help="Sleep interval between cycles in seconds (default: 1.0).",
    )
    return parser.parse_args()


def run_loop(*, cycles: int, max_seconds: float, interval: float):
    # Guardrails: never run an unbounded busy loop.
    cycles = max(1, int(cycles))
    interval = max(0.5, float(interval))
    max_seconds = float(max_seconds)

    # First load
    sync_strategies()

    executor = ThreadPoolExecutor(max_workers=4)
    started_at = time.monotonic()
    halted = False
    last_sync_time = time.monotonic()

    try:
        for _ in range(cycles):
            if max_seconds > 0 and (time.monotonic() - started_at) >= max_seconds:
                break

            # Global circuit breaker
            acc = mt5.account_info()
            if acc and acc.margin_level > 0 and acc.margin_level < 200:
                if not halted:
                    Logger.log("SYSTEM", "HALT", f"保证金过低 ({acc.margin_level}%)，暂停运行")
                    halted = True
                time.sleep(max(2.0, interval))
                continue
            halted = False

            # 优化：减少配置检查频率，避免频繁重载
            current_time = time.monotonic()
            if current_time - last_sync_time >= 2.0:  # 每2秒检查一次配置变更
                sync_strategies()
                last_sync_time = current_time

            # Batch fetch orders/positions once
            all_orders = mt5.orders_get()
            all_positions = mt5.positions_get()

            orders_by_magic = defaultdict(list)
            if all_orders:
                for o in all_orders:
                    orders_by_magic[o.magic].append(o)

            positions_by_magic = defaultdict(list)
            if all_positions:
                for p in all_positions:
                    positions_by_magic[p.magic].append(p)

            futures = []
            for magic, s in active_strategies.items():
                # 检查策略是否启用
                if not s.enabled:
                    continue
                    
                f = executor.submit(
                    s.update,
                    orders_list=orders_by_magic[magic],
                    positions_list=positions_by_magic[magic],
                )
                futures.append(f)

            for f in futures:
                f.result()

            time.sleep(interval)
    finally:
        executor.shutdown(wait=True)

if __name__ == "__main__":
    args = parse_args()
    if initialize_system():
        Logger.log("SYSTEM", "START", f"系统已就绪 (cycles={args.cycles}, max_seconds={args.max_seconds}, interval={args.interval})")
        try:
            run_loop(cycles=args.cycles, max_seconds=args.max_seconds, interval=args.interval)
        except KeyboardInterrupt:
            Logger.log("SYSTEM", "STOP", "手动停止")
        except Exception as e:
            Logger.log("SYSTEM", "ERROR", f"运行异常: {e}")
        finally:
            # 仅在程序正常退出时关闭MT5连接
            if mt5.terminal_info() is not None:
                mt5.shutdown()
                Logger.log("SYSTEM", "SHUTDOWN", "MT5连接已关闭")
    else:
        Logger.log("SYSTEM", "ERROR", "系统初始化失败")