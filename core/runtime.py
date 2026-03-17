"""运行时调度引擎：市场数据馈送、指标缓存与策略调度器。"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set, Tuple

import numpy as np
import MetaTrader5 as mt5

from core.logger import Logger

# ===========================================================================
# 常量定义
# ===========================================================================
MIN_INTERVAL_SECONDS = 0.5
RETRY_SLEEP_SECONDS = 2.0
SYNC_INTERVAL_SECONDS = 2.0
ACCOUNT_LOG_INTERVAL_SECONDS = 300.0
MAX_CONSECUTIVE_ERRORS = 10

ATR_MIN_LOOKBACK_MULTIPLIER = 5
ATR_MIN_LOOKBACK_ABSOLUTE = 50

# ===========================================================================
# 协议定义
# ===========================================================================
class BrokerProtocol(Protocol):
    @property
    def lock(self) -> RLock: ...

    def account_info(self) -> Optional[mt5.AccountInfo]: ...
    def terminal_info(self) -> Optional[mt5.TerminalInfo]: ...
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[np.ndarray]: ...
    def symbol_info_tick(self, symbol: str) -> Optional[mt5.Tick]: ...
    def orders_get(self) -> Optional[Tuple[mt5.Order, ...]]: ...
    def positions_get(self) -> Optional[Tuple[mt5.Position, ...]]: ...

# ===========================================================================
# 数据馈送（指标缓存）
# ===========================================================================
@dataclass(slots=True)
class _AtrState:
    last_value: float | None = None
    last_time: float = 0.0

@dataclass(slots=True)
class _RatesState:
    last_time: float = 0.0
    rates: np.ndarray | None = None

class DataFeed:
    """提供市场数据和指标计算，带缓存机制。"""

    def __init__(self, broker: BrokerProtocol) -> None:
        self._broker = broker
        self._atr_cache: Dict[Tuple[str, int, int, str, float], _AtrState] = {}
        # 恢复：该缓存字典为对外接口 get_rates 服务，策略层重度依赖
        self._rates_cache: Dict[Tuple[str, int, int], _RatesState] = {}
        self._lock = broker.lock 

    def get_atr(
        self,
        symbol: str,
        timeframe: int,
        period: int,
        mode: str,
        smooth: float,
        update_seconds: float,
    ) -> float | None:
        key = (symbol, timeframe, period, mode, smooth)
        state = self._atr_cache.setdefault(key, _AtrState())
        
        # 优化保留：直接内联时钟函数，降低高频栈帧开销
        now = time.monotonic()

        if (now - state.last_time) < update_seconds:
            return state.last_value

        lookback = max(period * ATR_MIN_LOOKBACK_MULTIPLIER, ATR_MIN_LOOKBACK_ABSOLUTE)
        rates = self._copy_rates(symbol, timeframe, lookback + 1)
        if rates is None or len(rates) < period + 1:
            return state.last_value

        rates = rates[:-1]
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        prev_closes = rates["close"][:-1]

        tr = np.maximum(
            highs - lows,
            np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes))
        )
        if len(tr) < period:
            return state.last_value

        raw_atr = self._calculate_raw_atr(tr, period, mode)
        new_value = self._smooth_atr(raw_atr, state.last_value, smooth)

        state.last_value = new_value
        state.last_time = now
        return new_value

    def get_rates(
        self,
        symbol: str,
        timeframe: int,
        count: int,
        *,
        cache_seconds: float = 1.0,
        min_ratio: float = 0.7,
    ) -> np.ndarray | None:
        """获取 K 线数据（缓存优先）。该接口暴露给外部策略对象使用。"""
        key = (symbol, timeframe, count)
        state = self._rates_cache.setdefault(key, _RatesState())
        now = time.monotonic()

        if state.rates is not None and (now - state.last_time) < cache_seconds:
            return state.rates

        rates = self._copy_rates(symbol, timeframe, count)
        min_count = int(count * min_ratio)
        if rates is None or len(rates) < min_count:
            return state.rates if state.rates is not None else None

        state.rates = rates
        state.last_time = now
        return rates

    def _copy_rates(self, symbol: str, timeframe: int, count: int) -> np.ndarray | None:
        with self._lock:
            return self._broker.copy_rates_from_pos(symbol, timeframe, 0, count)

    @staticmethod
    def _calculate_raw_atr(tr: np.ndarray, period: int, mode: str) -> float:
        if mode == "sma":
            return float(np.mean(tr[-period:]))

        # [P-11] 纯 NumPy 射量化 EMA/Wilder：用 dot 一次性计算最终标量结果
        # final = y0*d^n + alpha * dot(tail, [d^(n-1), ..., d^0])
        current_atr = float(np.mean(tr[:period]))
        alpha = 2.0 / (period + 1.0) if mode == "ema" else 1.0 / period
        tail = tr[period:].astype(float)
        n = len(tail)
        if n == 0:
            return current_atr
        decay = 1.0 - alpha
        # d^(n-1), d^(n-2), ..., d^0（每个 tail 元素对应的衰减权重）
        decay_powers_rev = decay ** np.arange(n - 1, -1, -1)
        return float(current_atr * (decay ** n) + alpha * np.dot(tail, decay_powers_rev))

    @staticmethod
    def _smooth_atr(raw_atr: float, last_value: float | None, smooth: float) -> float:
        if not smooth or last_value is None:
            return raw_atr
        if 0 < smooth < 1:
            return (last_value * (1.0 - smooth)) + (raw_atr * smooth)
        return raw_atr

# ===========================================================================
# 策略上下文与循环状态
# ===========================================================================
@dataclass(slots=True)
class StrategyContext:
    tick: Optional[mt5.Tick]
    orders: List[mt5.Order]
    positions: List[mt5.Position]
    atr: float | None = None

@dataclass(slots=True)
class _LoopState:
    last_sync_time: float
    last_account_log_time: float = 0.0
    consecutive_errors: int = 0
    halted: bool = False

# ===========================================================================
# 调度器引擎
# ===========================================================================
class Runner:
    """主调度循环，负责同步策略、执行交易逻辑。"""

    def __init__(self, broker: BrokerProtocol, strategy_manager: Any) -> None:
        self._broker = broker
        self._strategy_manager = strategy_manager
        self._datafeed = strategy_manager.datafeed
        queue_flag = str(os.getenv("INV_USE_ACTION_QUEUE", "0")).strip().lower()
        self._use_action_queue = queue_flag in {"1", "true", "yes", "y", "on"}
        if self._use_action_queue:
            Logger.log("SYSTEM", "WARN", "INV_USE_ACTION_QUEUE is enabled; queued execution may delay state convergence")

    def run(self, *, cycles: int, max_seconds: float, interval: float) -> None:
        cycles = max(1, int(cycles))
        interval = max(MIN_INTERVAL_SECONDS, float(interval))
        started_at = time.monotonic()

        self._strategy_manager.sync()
        loop_state = _LoopState(last_sync_time=time.monotonic())

        for _ in range(cycles):
            now = time.monotonic()
            if max_seconds > 0 and (now - started_at) >= max_seconds:
                Logger.log("系统", "信息", f"达到最大运行时间 {max_seconds:.1f} 秒，循环终止")
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

            enabled_strategies = [s for s in self._strategy_manager.active.values() if s.enabled]
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

    def _fetch_system_info(self) -> Tuple[Optional[mt5.AccountInfo], Optional[mt5.TerminalInfo]]:
        with self._broker.lock:
            return self._broker.account_info(), self._broker.terminal_info()

    def _handle_system_info_error(self, loop_state: _LoopState, interval: float) -> bool:
        loop_state.consecutive_errors += 1
        Logger.log(
            "系统",
            "警告",
            f"无法获取账户/终端信息 (尝试 {loop_state.consecutive_errors}/{MAX_CONSECUTIVE_ERRORS})",
        )
        if loop_state.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            Logger.log("系统", "致命错误", "连续多次连接终端失败，为防止挂机死循环，程序主动停机")
            return True
        time.sleep(max(RETRY_SLEEP_SECONDS, interval))
        return False

    def _should_pause_for_account_guard(
        self,
        account_info: mt5.AccountInfo,
        loop_state: _LoopState,
        interval: float,
        now: float,
    ) -> bool:
        def _fmt_num(value: Any, width: int = 10) -> str:
            try:
                return f"{float(value):>{width}.2f}"
            except (TypeError, ValueError):
                return f"{'N/A':>{width}}"

        margin_level_raw = getattr(account_info, "margin_level", None)
        try:
            margin_level = float(margin_level_raw)
        except (TypeError, ValueError):
            margin_level = None

        if now - loop_state.last_account_log_time > ACCOUNT_LOG_INTERVAL_SECONDS:
            margin_level_display = (
                f"{margin_level:>9.2f}%" if margin_level is not None else f"{'N/A':>9}"
            )
            Logger.log(
                "系统",
                "资金播报",
                (
                    f"余额: {_fmt_num(getattr(account_info, 'balance', None))} | "
                    f"净值: {_fmt_num(getattr(account_info, 'equity', None))} | "
                    f"预付款: {_fmt_num(getattr(account_info, 'margin', None))} | "
                    f"比例: {margin_level_display}"
                ),
            )
            loop_state.last_account_log_time = now

        if margin_level is not None and 0 < margin_level < 200:
            if not loop_state.halted:
                Logger.log("系统", "风控拦截", f"保证金比例极危 ({margin_level:>6.2f}%)，全策略暂停运行")
                loop_state.halted = True
            time.sleep(max(RETRY_SLEEP_SECONDS, interval))
            return True

        loop_state.halted = False
        return False

    def _sync_if_needed(self, loop_state: _LoopState, now: float) -> None:
        if now - loop_state.last_sync_time >= SYNC_INTERVAL_SECONDS:
            self._strategy_manager.sync()
            loop_state.last_sync_time = now

    def _fetch_order_position_maps(
        self,
        enabled_keys: Set[Tuple[int, str]],
    ) -> Tuple[Dict[Tuple[int, str], List[Any]], Dict[Tuple[int, str], List[Any]]]:
        with self._broker.lock:
            all_orders = self._broker.orders_get()
            all_positions = self._broker.positions_get()
        return (
            self._group_by_strategy_key(all_orders, enabled_keys),
            self._group_by_strategy_key(all_positions, enabled_keys),
        )

    @staticmethod
    def _group_by_strategy_key(
        items: Optional[Iterable[Any]],
        enabled_keys: Set[Tuple[int, str]],
    ) -> Dict[Tuple[int, str], List[Any]]:
        if not items:
            return {}
        grouped = defaultdict(list)
        for item in items:
            key = (item.magic, item.symbol)
            if key in enabled_keys:
                grouped[key].append(item)
        return grouped

    def _fetch_ticks(self, strategies: List[Any]) -> Dict[str, Optional[mt5.Tick]]:
        symbols = {strategy.symbol for strategy in strategies}
        with self._broker.lock:
            return {symbol: self._broker.symbol_info_tick(symbol) for symbol in symbols}

    def _run_strategy_cycle(
        self,
        strategy: Any,
        magic: int,
        ticks_by_symbol: Dict[str, Optional[mt5.Tick]],
        orders_by_key: Dict[Tuple[int, str], List[Any]],
        positions_by_key: Dict[Tuple[int, str], List[Any]],
    ) -> None:
        actions = []
        action_collector = actions if self._use_action_queue else None
        cycle_failed = False
        try:
            ctx = self._build_strategy_context(
                strategy,
                magic,
                ticks_by_symbol,
                orders_by_key,
                positions_by_key,
            )

            # 保留原生 CPython 高速 hasattr 优化，移除多余字典结构
            if hasattr(strategy, "on_tick"):
                strategy.on_tick(ctx, action_collector=action_collector)
            else:
                strategy.update(
                    orders_list=ctx.orders,
                    positions_list=ctx.positions,
                    tick=ctx.tick,
                    orders_filtered=True,
                    positions_filtered=True,
                    atr=ctx.atr,
                    action_collector=action_collector,
                )
        except Exception as exc:
            cycle_failed = True
            Logger.log(strategy.symbol, "异常", f"策略执行生命周期内发生崩溃 (magic={strategy.magic}): {exc}")

        if self._use_action_queue and (not cycle_failed):
            self._flush_actions(strategy, actions)

    def _build_strategy_context(
        self,
        strategy: Any,
        magic: int,
        ticks_by_symbol: Dict[str, Optional[mt5.Tick]],
        orders_by_key: Dict[Tuple[int, str], List[Any]],
        positions_by_key: Dict[Tuple[int, str], List[Any]],
    ) -> StrategyContext:
        atr = None
        if strategy.use_atr:
            atr = self._datafeed.get_atr(
                symbol=strategy.symbol,
                timeframe=strategy._resolve_timeframe(),
                period=strategy.atr_period,
                mode=strategy.atr_mode,
                smooth=strategy.atr_smooth,
                update_seconds=strategy.atr_update_seconds,
            )
        return StrategyContext(
            tick=ticks_by_symbol.get(strategy.symbol),
            orders=orders_by_key.get((magic, strategy.symbol), []),
            positions=positions_by_key.get((magic, strategy.symbol), []),
            atr=atr,
        )

    def _flush_actions(self, strategy: Any, actions: List[Any]) -> None:
        if not actions:
            return

        # 快照并清空队列，同时重置 _action_collector 为 None
        # 防止 _dispatch_request → _append_action_if_queued 将请求重新写入 actions
        # 列表，导致 for 循环无限追加处理同一张单（死循环挂单检查）
        pending = list(actions)
        actions.clear()
        strategy._action_collector = None

        for request in pending:
            if not isinstance(request, dict):
                continue

            action = request.get("action")
            is_trade_action = action in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING)
            try:
                if is_trade_action:
                    result = strategy._send_with_fillings(request)
                else:
                    with self._broker.lock:
                        result = mt5.order_send(request)
            except Exception as exc:
                Logger.log(
                    strategy.symbol,
                    "EXCEPTION",
                    f"flush_actions order_send exception (magic={strategy.magic}): {exc}",
                )
                continue

            if result is None:
                Logger.log(
                    strategy.symbol,
                    "错误",
                    f"order_send 底层返回 None，C-API 交互异常 (magic={strategy.magic})。代码: {mt5.last_error()}",
                )
                continue

            if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                if is_trade_action and hasattr(strategy, "_handle_order_error"):
                    try:
                        strategy._handle_order_error(
                            result.retcode,
                            getattr(result, "comment", ""),
                            request.get("price"),
                        )
                    except Exception as exc:
                        Logger.log(
                            strategy.symbol,
                            "EXCEPTION",
                            f"_handle_order_error failed (magic={strategy.magic}): {exc}",
                        )
                else:
                    Logger.log(
                        strategy.symbol,
                        "拒单",
                        f"RC: {result.retcode} | {getattr(result, 'comment', '无附言')} | magic={strategy.magic}",
                    )
                continue

            if is_trade_action and hasattr(strategy, "_on_order_submit_success"):
                try:
                    strategy._on_order_submit_success()
                except Exception:
                    pass

__all__ = ["DataFeed", "Runner"]
