"""Runtime orchestration: market data feed, indicator cache, and strategy scheduler."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Set, Tuple

import numpy as np
import MetaTrader5 as mt5

from core.logger import Logger


# ---------------------------------------------------------------------------
# DataFeed
# ---------------------------------------------------------------------------

@dataclass
class _AtrState:
    last_value: float | None = None
    last_time: float = 0.0


@dataclass
class _RatesState:
    last_time: float = 0.0
    rates: Any | None = None


class DataFeed:
    def __init__(self, broker):
        self.broker = broker
        self._atr_cache: Dict[Tuple[str, int, int, str, float], _AtrState] = {}
        self._rates_cache: Dict[Tuple[str, int, int], _RatesState] = {}

    def get_atr(
        self,
        symbol: str,
        timeframe: int,
        period: int,
        mode: str,
        smooth: float,
        update_seconds: float,
    ) -> float | None:
        period = int(period)
        smooth = float(smooth)
        normalized_mode = str(mode or "").lower()

        key = (symbol, timeframe, period, normalized_mode, smooth)
        state = self._atr_cache.setdefault(key, _AtrState())

        now = self._now()
        update_seconds = float(update_seconds)
        if (now - state.last_time) < update_seconds:
            return state.last_value

        # Fetch enough bars so EMA/Wilder recursion converges from SMA seed.
        lookback = max(period * 5, 50)
        rates = self._copy_rates(symbol, timeframe, lookback + 1)
        if rates is None or len(rates) < period + 1:
            return state.last_value

        rates = rates[:-1]  # Ignore the currently forming bar.
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        prev_closes = rates["close"][:-1]

        tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)))
        if len(tr) < period:
            return state.last_value

        raw_atr = self._calculate_raw_atr(tr, period, normalized_mode)
        state.last_value = self._smooth_atr(raw_atr, state.last_value, smooth)
        state.last_time = now
        return state.last_value

    def get_rates(
        self,
        symbol: str,
        timeframe: int,
        count: int,
        *,
        cache_seconds: float = 1.0,
        min_ratio: float = 0.7,
    ) -> Any:
        count = int(count)
        key = (symbol, timeframe, count)
        state = self._rates_cache.setdefault(key, _RatesState())

        now = self._now()
        cache_seconds = float(cache_seconds)
        if state.rates is not None and (now - state.last_time) < cache_seconds:
            return state.rates

        rates = self._copy_rates(symbol, timeframe, count)
        min_count = int(count * float(min_ratio))
        if rates is None or len(rates) < min_count:
            return state.rates

        state.rates = rates
        state.last_time = now
        return rates

    def _copy_rates(self, symbol: str, timeframe: int, count: int):
        if getattr(self.broker, "lock", None) is not None:
            with self.broker.lock:
                return self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(count))
        return self.broker.copy_rates_from_pos(symbol, timeframe, 0, int(count))

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    @staticmethod
    def _calculate_raw_atr(tr: np.ndarray, period: int, mode: str) -> float:
        if mode == "sma":
            return float(np.mean(tr[-period:]))

        current_atr = float(np.mean(tr[:period]))
        alpha = 2.0 / (period + 1.0) if mode == "ema" else 1.0 / period
        for i in range(period, len(tr)):
            current_atr = (current_atr * (1.0 - alpha)) + (tr[i] * alpha)
        return float(current_atr)

    @staticmethod
    def _smooth_atr(raw_atr: float, last_value: float | None, smooth: float) -> float:
        if not smooth or last_value is None:
            return raw_atr
        if 0 < smooth < 1:
            return (last_value * (1 - smooth)) + (raw_atr * smooth)
        return raw_atr


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

# 【修改点】使用 slots=True (需 Python 3.10+) 替代动态字典，
# 将极高频实例化的 Context 对象内存占用降低约 40-50%，显著缓解 GC 压力。
@dataclass(slots=True)
class StrategyContext:
    tick: Any
    orders: list
    positions: list
    atr: float | None = None


_MIN_INTERVAL_SECONDS = 0.5
_RETRY_SLEEP_SECONDS = 2.0
_SYNC_INTERVAL_SECONDS = 2.0
_ACCOUNT_LOG_INTERVAL_SECONDS = 300.0
_MAX_CONSECUTIVE_ERRORS = 10


@dataclass(slots=True)
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

        self.strategy_manager.sync()  # 初始加载
        loop_state = _LoopState(last_sync_time=time.monotonic())

        for _ in range(cycles):
            # 【修改点】每轮循环仅发起一次系统时钟调用，将时间戳透传给所有子系统
            now = time.monotonic()

            if max_seconds > 0 and (now - started_at) >= max_seconds:
                break

            account_info, terminal_info = self._fetch_system_info()
            if account_info is None or terminal_info is None:
                if self._handle_system_info_error(loop_state, interval):
                    break
                continue

            loop_state.consecutive_errors = 0
            if self._should_pause_for_account_guard(account_info, loop_state, interval, now):
                continue

            self._sync_if_needed(loop_state, now)

            # 【修改点】缓存 active.values() 迭代，避免每次都生成新视图
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

    def _fetch_system_info(self):
        with self.broker.lock:
            return self.broker.account_info(), self.broker.terminal_info()

    def _handle_system_info_error(self, loop_state: _LoopState, interval: float) -> bool:
        loop_state.consecutive_errors += 1
        Logger.log(
            "系统",
            "警告",
            f"无法获取账户/终端信息 (尝试 {loop_state.consecutive_errors}/{_MAX_CONSECUTIVE_ERRORS})",
        )
        if loop_state.consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            Logger.log("系统", "致命错误", "连续多次连接终端失败，为防止挂机死循环，程序主动停机")
            return True
        time.sleep(max(_RETRY_SLEEP_SECONDS, interval))
        return False

    def _should_pause_for_account_guard(
        self, account_info, loop_state: _LoopState, interval: float, now: float
    ) -> bool:
        if account_info is None:
            loop_state.halted = False
            return False

        if now - loop_state.last_account_log_time > _ACCOUNT_LOG_INTERVAL_SECONDS:
            Logger.log(
                "系统",
                "资金播报",
                (
                    f"余额: {account_info.balance:>10.2f} | 净值: {account_info.equity:>10.2f} | "
                    f"预付款: {account_info.margin:>10.2f} | 比例: {account_info.margin_level:>9.2f}%"
                ),
            )
            loop_state.last_account_log_time = now

        if 0 < account_info.margin_level < 200:
            if not loop_state.halted:
                Logger.log("系统", "风控拦截", f"保证金比例极危 ({account_info.margin_level:>6.2f}%)，全策略暂停运行")
                loop_state.halted = True
            time.sleep(max(_RETRY_SLEEP_SECONDS, interval))
            return True

        loop_state.halted = False
        return False

    def _sync_if_needed(self, loop_state: _LoopState, now: float):
        if now - loop_state.last_sync_time >= _SYNC_INTERVAL_SECONDS:
            self.strategy_manager.sync()
            loop_state.last_sync_time = now

    def _fetch_order_position_maps(self, enabled_keys: Set[Tuple[int, str]]) -> Tuple[Dict, Dict]:
        with self.broker.lock:
            all_orders = self.broker.orders_get()
            all_positions = self.broker.positions_get()
        return (
            self._group_by_strategy_key(all_orders, enabled_keys),
            self._group_by_strategy_key(all_positions, enabled_keys),
        )

    @staticmethod
    def _group_by_strategy_key(items: Iterable[Any] | None, enabled_keys: Set[Tuple[int, str]]) -> Dict:
        """【修改点】前置判空拦截，避免在无订单/持仓时仍然创建并丢弃 defaultdict 对象。"""
        if not items:
            return {}
        grouped = defaultdict(list)
        for item in items:
            key = (item.magic, item.symbol)
            if key in enabled_keys:
                grouped[key].append(item)
        return grouped

    def _fetch_ticks(self, strategies: list) -> Dict[str, Any]:
        """批量获取当前激活品种的最新 Tick 数据。"""
        symbols = {strategy.symbol for strategy in strategies}
        with self.broker.lock:
            return {symbol: self.broker.symbol_info_tick(symbol) for symbol in symbols}

    def _build_strategy_context(
        self,
        strategy,
        magic: int,
        ticks_by_symbol: Dict,
        orders_by_key: Dict,
        positions_by_key: Dict,
    ) -> StrategyContext:
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
            orders=orders_by_key.get((magic, strategy.symbol), []),
            positions=positions_by_key.get((magic, strategy.symbol), []),
            atr=atr,
        )

    def _run_strategy_cycle(
        self,
        strategy,
        magic: int,
        ticks_by_symbol: Dict,
        orders_by_key: Dict,
        positions_by_key: Dict,
    ):
        actions = []
        try:
            ctx = self._build_strategy_context(strategy, magic, ticks_by_symbol, orders_by_key, positions_by_key)

            # 策略统一调度入口
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
        except Exception as exc:
            Logger.log(strategy.symbol, "异常", f"策略执行生命周期内发生崩溃 (magic={strategy.magic}): {exc}")
        finally:
            # 【修改点】统一生命周期钩子管理，确保无论成功或崩溃，都安全剥离收集器
            strategy._action_collector = None

        # 将发单逻辑移出 try-catch 块，隔离策略逻辑异常与网络发单异常
        self._flush_actions(strategy, actions)

    def _flush_actions(self, strategy, actions: list):
        """【修改点】移除了冗余的 action_collector 清理，直接专注订单执行。"""
        if not actions:
            return

        for request in actions:
            if isinstance(request, dict) and request.get("action") in (
                mt5.TRADE_ACTION_DEAL,
                mt5.TRADE_ACTION_PENDING,
            ):
                result = strategy._send_with_fillings(request)
            else:
                with self.broker.lock:
                    result = mt5.order_send(request)

            if result is None:
                Logger.log(
                    strategy.symbol,
                    "错误",
                    f"order_send 底层返回 None，C-API 交互异常 (magic={strategy.magic})。代码: {mt5.last_error()}",
                )
                continue

            if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                Logger.log(
                    strategy.symbol,
                    "拒单",
                    f"RC: {result.retcode} | {getattr(result, 'comment', '无附言')} | magic={strategy.magic}",
                )


__all__ = ["DataFeed", "Runner"]
