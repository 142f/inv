import time
from collections import defaultdict
import MetaTrader5 as mt5
from core.logger import Logger


class Runner:
    def __init__(self, mt5_client, strategy_manager):
        self.mt5_client = mt5_client
        self.strategy_manager = strategy_manager

    def run(self, *, cycles: int, max_seconds: float, interval: float):
        cycles = max(1, int(cycles))
        interval = max(0.5, float(interval))
        started_at = time.monotonic()
        halted = False
        self.strategy_manager.sync()  # initial load
        last_sync_time = time.monotonic()
        last_account_log_time = 0.0
        consecutive_errors = 0
        max_consecutive_errors = 10

        for _ in range(cycles):
            if max_seconds > 0 and (time.monotonic() - started_at) >= max_seconds:
                break

            with self.mt5_client.lock:
                acc = mt5.account_info()
                term = mt5.terminal_info()

            if acc is None or term is None:
                consecutive_errors += 1
                Logger.log(
                    "SYSTEM",
                    "WARN",
                    f"无法获取账户/终端信息 (尝试 {consecutive_errors}/{max_consecutive_errors})",
                )
                if consecutive_errors >= max_consecutive_errors:
                    Logger.log("SYSTEM", "CRITICAL", "连续多次连接失败，为防止死循环，程序停止")
                    break
                time.sleep(max(2.0, interval))
                continue

            consecutive_errors = 0

            if acc:
                now = time.monotonic()
                if now - last_account_log_time > 300:
                    Logger.log(
                        "SYSTEM",
                        "ACCOUNT",
                        f"余额: {acc.balance:.2f} | 净值: {acc.equity:.2f} | 预付款: {acc.margin:.2f} | 比例: {acc.margin_level:.2f}%",
                    )
                    last_account_log_time = now

                if acc.margin_level > 0 and acc.margin_level < 200:
                    if not halted:
                        Logger.log("SYSTEM", "HALT", f"保证金过低 ({acc.margin_level}%)，暂停运行")
                        halted = True
                    time.sleep(max(2.0, interval))
                    continue
            halted = False

            now = time.monotonic()
            if now - last_sync_time >= 2.0:
                self.strategy_manager.sync()
                last_sync_time = now

            with self.mt5_client.lock:
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

            for magic, strategy in self.strategy_manager.active.items():
                if not strategy.enabled:
                    continue

                try:
                    strategy.update(
                        orders_list=orders_by_magic[magic],
                        positions_list=positions_by_magic[magic],
                    )
                except Exception as exc:
                    Logger.log(strategy.symbol, "ERROR", f"策略执行异常: {exc}")

            time.sleep(interval)
