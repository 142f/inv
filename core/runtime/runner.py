import time
from collections import defaultdict

import MetaTrader5 as mt5

from core.logger import Logger
from core.runtime.context import StrategyContext
from core.runtime.datafeed import DataFeed


class Runner:
    def __init__(self, broker, strategy_manager):
        self.broker = broker
        self.strategy_manager = strategy_manager
        self.datafeed = DataFeed(broker)

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

            with self.broker.lock:
                acc = self.broker.account_info()
                term = self.broker.terminal_info()

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
                        f"余额: {acc.balance:>10.2f} | 净值: {acc.equity:>10.2f} | 预付款: {acc.margin:>10.2f} | 比例: {acc.margin_level:>9.2f}%",
                    )
                    last_account_log_time = now

                if acc.margin_level > 0 and acc.margin_level < 200:
                    if not halted:
                        Logger.log("SYSTEM", "HALT", f"保证金过低({acc.margin_level:>6.2f}%)，暂停运行")
                        halted = True
                    time.sleep(max(2.0, interval))
                    continue
            halted = False

            now = time.monotonic()
            if now - last_sync_time >= 2.0:
                self.strategy_manager.sync()
                last_sync_time = now

            enabled_strategies = [s for s in self.strategy_manager.active.values() if s.enabled]
            if not enabled_strategies:
                time.sleep(interval)
                continue

            with self.broker.lock:
                all_orders = self.broker.orders_get()
                all_positions = self.broker.positions_get()

            orders_by_key = defaultdict(list)
            if all_orders:
                for o in all_orders:
                    orders_by_key[(o.magic, o.symbol)].append(o)

            positions_by_key = defaultdict(list)
            if all_positions:
                for p in all_positions:
                    positions_by_key[(p.magic, p.symbol)].append(p)

            # 预取 tick：同一 symbol 多策略时避免重复调用 mt5.symbol_info_tick
            symbols = {s.symbol for s in enabled_strategies}
            with self.broker.lock:
                ticks_by_symbol = {sym: self.broker.symbol_info_tick(sym) for sym in symbols}

            for magic, strategy in self.strategy_manager.active.items():
                if not strategy.enabled:
                    continue

                try:
                    actions = []
                    atr = None
                    if strategy.use_atr:
                        timeframe = strategy._resolve_timeframe()
                        atr = self.datafeed.get_atr(
                            strategy.symbol,
                            timeframe,
                            strategy.atr_period,
                            strategy.atr_mode,
                            strategy.atr_smooth,
                            strategy.atr_update_seconds,
                        )
                    ctx = StrategyContext(
                        tick=ticks_by_symbol.get(strategy.symbol),
                        orders=orders_by_key[(magic, strategy.symbol)],
                        positions=positions_by_key[(magic, strategy.symbol)],
                        account=acc,
                        atr=atr,
                        now=time.time(),
                    )
                    if hasattr(strategy, "on_tick"):
                        strategy.on_tick(ctx, action_collector=actions)
                    else:
                        strategy.update(
                            orders_list=ctx.orders,
                            positions_list=ctx.positions,
                            tick=ctx.tick,
                            orders_filtered=True,
                            positions_filtered=True,
                            atr=ctx.atr,
                            action_collector=actions,
                        )
                    if actions:
                        for request in actions:
                            with self.broker.lock:
                                result = self.broker.order_send(request)
                            if result is None:
                                Logger.log(strategy.symbol, "ERROR", f"order_send returned None. Error: {mt5.last_error()}")
                                continue
                            if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                                Logger.log(
                                    strategy.symbol,
                                    "ORDER_FAIL",
                                    f"RC: {result.retcode} | {getattr(result, 'comment', '')}",
                                )
                    strategy._action_collector = None
                except Exception as exc:
                    Logger.log(strategy.symbol, "ERROR", f"策略执行异常: {exc}")

            time.sleep(interval)

