from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

import MetaTrader5 as mt5

from core.logger import Logger
from core.runtime.context import StrategyContext


_MIN_INTERVAL_SECONDS = 0.5
_RETRY_SLEEP_SECONDS = 2.0
_SYNC_INTERVAL_SECONDS = 2.0
_ACCOUNT_LOG_INTERVAL_SECONDS = 300.0
_MAX_CONSECUTIVE_ERRORS = 10


@dataclass
class _LoopState:
    last_sync_time: float
    last_account_log_time: float = 0.0
    consecutive_errors: int = 0
    halted: bool = False


class Runner:
    def __init__(self, broker, strategy_manager):
        self.broker = broker
        self.strategy_manager = strategy_manager
        self.datafeed = strategy_manager.datafeed

    def run(self, *, cycles: int, max_seconds: float, interval: float):
        cycles = max(1, int(cycles))
        interval = max(_MIN_INTERVAL_SECONDS, float(interval))
        started_at = time.monotonic()

        self.strategy_manager.sync()  # initial load
        loop_state = _LoopState(last_sync_time=time.monotonic())

        for _ in range(cycles):
            if self._reached_time_limit(started_at, max_seconds):
                break

            account_info, terminal_info = self._fetch_system_info()
            if account_info is None or terminal_info is None:
                should_stop = self._handle_system_info_error(loop_state, interval)
                if should_stop:
                    break
                continue

            loop_state.consecutive_errors = 0
            if self._should_pause_for_account_guard(account_info, loop_state, interval):
                continue

            self._sync_if_needed(loop_state)

            enabled_strategies = [s for s in self.strategy_manager.active.values() if s.enabled]
            if not enabled_strategies:
                time.sleep(interval)
                continue

            enabled_keys = {(s.magic, s.symbol) for s in enabled_strategies}
            orders_by_key, positions_by_key = self._fetch_order_position_maps(enabled_keys)
            ticks_by_symbol = self._fetch_ticks(enabled_strategies)

            for strategy in enabled_strategies:
                self._run_strategy_cycle(
                    strategy,
                    strategy.magic,
                    ticks_by_symbol,
                    orders_by_key,
                    positions_by_key,
                )

            time.sleep(interval)

    @staticmethod
    def _reached_time_limit(started_at: float, max_seconds: float) -> bool:
        return max_seconds > 0 and (time.monotonic() - started_at) >= max_seconds

    def _fetch_system_info(self):
        with self.broker.lock:
            return self.broker.account_info(), self.broker.terminal_info()

    def _handle_system_info_error(self, loop_state: _LoopState, interval: float) -> bool:
        loop_state.consecutive_errors += 1
        Logger.log(
            "SYSTEM",
            "WARN",
            f"无法获取账户/终端信息 (尝试 {loop_state.consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS})",
        )
        if loop_state.consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            Logger.log("SYSTEM", "CRITICAL", "连续多次连接失败，为防止死循环，程序停止")
            return True
        time.sleep(max(_RETRY_SLEEP_SECONDS, interval))
        return False

    def _should_pause_for_account_guard(self, account_info, loop_state: _LoopState, interval: float) -> bool:
        if account_info is None:
            loop_state.halted = False
            return False

        now = time.monotonic()
        if now - loop_state.last_account_log_time > _ACCOUNT_LOG_INTERVAL_SECONDS:
            Logger.log(
                "SYSTEM",
                "ACCOUNT",
                (
                    f"余额: {account_info.balance:>10.2f} | 净值: {account_info.equity:>10.2f} | "
                    f"预付款: {account_info.margin:>10.2f} | 比例: {account_info.margin_level:>9.2f}%"
                ),
            )
            loop_state.last_account_log_time = now

        if account_info.margin_level > 0 and account_info.margin_level < 200:
            if not loop_state.halted:
                Logger.log("SYSTEM", "HALT", f"保证金过低({account_info.margin_level:>6.2f}%)，暂停运行")
                loop_state.halted = True
            time.sleep(max(_RETRY_SLEEP_SECONDS, interval))
            return True

        loop_state.halted = False
        return False

    def _sync_if_needed(self, loop_state: _LoopState):
        now = time.monotonic()
        if now - loop_state.last_sync_time >= _SYNC_INTERVAL_SECONDS:
            self.strategy_manager.sync()
            loop_state.last_sync_time = now

    def _fetch_order_position_maps(self, enabled_keys):
        with self.broker.lock:
            all_orders = self.broker.orders_get()
            all_positions = self.broker.positions_get()
        return (
            self._group_by_strategy_key(all_orders, enabled_keys),
            self._group_by_strategy_key(all_positions, enabled_keys),
        )

    @staticmethod
    def _group_by_strategy_key(items: Iterable[Any] | None, enabled_keys):
        grouped = defaultdict(list)
        if not items:
            return grouped
        for item in items:
            key = (item.magic, item.symbol)
            if key in enabled_keys:
                grouped[key].append(item)
        return grouped

    def _fetch_ticks(self, strategies):
        symbols = {strategy.symbol for strategy in strategies}
        with self.broker.lock:
            return {symbol: self.broker.symbol_info_tick(symbol) for symbol in symbols}

    def _build_strategy_context(self, strategy, magic, ticks_by_symbol, orders_by_key, positions_by_key):
        atr = None
        if strategy.use_atr:
            atr = self.datafeed.get_atr(
                strategy.symbol,
                strategy._resolve_timeframe(),
                strategy.atr_period,
                strategy.atr_mode,
                strategy.atr_smooth,
                strategy.atr_update_seconds,
            )
        return StrategyContext(
            tick=ticks_by_symbol.get(strategy.symbol),
            orders=orders_by_key[(magic, strategy.symbol)],
            positions=positions_by_key[(magic, strategy.symbol)],
            atr=atr,
        )

    def _run_strategy_cycle(self, strategy, magic, ticks_by_symbol, orders_by_key, positions_by_key):
        try:
            actions = []
            ctx = self._build_strategy_context(strategy, magic, ticks_by_symbol, orders_by_key, positions_by_key)
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
            self._flush_actions(strategy, actions)
        except Exception as exc:
            Logger.log(strategy.symbol, "ERROR", f"策略执行异常 (magic={strategy.magic}): {exc}")
        finally:
            # Ensure collector is always detached.
            strategy._action_collector = None

    def _flush_actions(self, strategy, actions):
        if not actions:
            return

        # Send queued requests with the strategy's fill-mode fallback.
        strategy._action_collector = None
        for request in actions:
            if isinstance(request, dict) and request.get("action") in (
                mt5.TRADE_ACTION_DEAL,
                mt5.TRADE_ACTION_PENDING,
            ):
                result = strategy._send_with_fillings(request)
            else:
                with self.broker.lock:
                    result = self.broker.order_send(request)

            if result is None:
                Logger.log(
                    strategy.symbol,
                    "ERROR",
                    f"order_send returned None (magic={strategy.magic}). Error: {mt5.last_error()}",
                )
                continue

            if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                Logger.log(
                    strategy.symbol,
                    "ORDER_FAIL",
                    f"RC: {result.retcode} | {getattr(result, 'comment', '')} | magic={strategy.magic}",
                )
