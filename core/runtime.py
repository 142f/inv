"""运行时调度引擎：市场数据馈送、指标缓存与策略调度器。"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
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
# 协议定义（依赖抽象，便于测试）
# ===========================================================================
class BrokerProtocol(Protocol):
    """定义 Broker 必须实现的方法（MT5 接口子集）。"""
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
        self._rates_cache: Dict[Tuple[str, int, int], _RatesState] = {}
        self._lock = broker.lock  # 直接引用，避免重复 getattr

    def get_atr(
        self,
        symbol: str,
        timeframe: int,
        period: int,
        mode: str,
        smooth: float,
        update_seconds: float,
    ) -> float | None:
        """获取 ATR 值（缓存优先）。"""
        key = (symbol, timeframe, period, mode, smooth)
        state = self._atr_cache.setdefault(key, _AtrState())
        now = self._now()

        # 缓存有效则直接返回
        if (now - state.last_time) < update_seconds:
            return state.last_value

        # 计算所需 K 线数量（确保平滑算法收敛）
        lookback = max(period * ATR_MIN_LOOKBACK_MULTIPLIER, ATR_MIN_LOOKBACK_ABSOLUTE)
        rates = self._copy_rates(symbol, timeframe, lookback + 1)
        if rates is None or len(rates) < period + 1:
            return state.last_value

        # 忽略未闭合的当前 K 线
        rates = rates[:-1]
        highs = rates["high"][1:]
        lows = rates["low"][1:]
        prev_closes = rates["close"][:-1]

        # 向量化计算 True Range
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
        """获取 K 线数据（缓存优先）。"""
        key = (symbol, timeframe, count)
        state = self._rates_cache.setdefault(key, _RatesState())
        now = self._now()

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
        """线程安全的 K 线拉取。"""
        with self._lock:
            return self._broker.copy_rates_from_pos(symbol, timeframe, 0, count)

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    @staticmethod
    def _calculate_raw_atr(tr: np.ndarray, period: int, mode: str) -> float:
        """计算原始 ATR（SMA / EMA / Wilders）。"""
        if mode == "sma":
            return float(np.mean(tr[-period:]))

        # 将 numpy 数组转为列表以避免后续循环中的标量拆箱开销
        tr_list = tr.tolist()
        current_atr = sum(tr_list[:period]) / period
        alpha = 2.0 / (period + 1.0) if mode == "ema" else 1.0 / period  # wilders

        for i in range(period, len(tr_list)):
            current_atr = (current_atr * (1.0 - alpha)) + (tr_list[i] * alpha)
        return float(current_atr)

    @staticmethod
    def _smooth_atr(raw_atr: float, last_value: float | None, smooth: float) -> float:
        """对 ATR 进行指数平滑（若启用）。"""
        if not smooth or last_value is None:
            return raw_atr
        if 0 < smooth < 1:
            return (last_value * (1.0 - smooth)) + (raw_atr * smooth)
        return raw_atr

# ===========================================================================
# 策略上下文
# ===========================================================================
@dataclass(slots=True)
class StrategyContext:
    tick: Optional[mt5.Tick]
    orders: List[mt5.Order]
    positions: List[mt5.Position]
    atr: float | None = None

# ===========================================================================
# 循环状态
# ===========================================================================
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
        self._datafeed = strategy_manager.datafeed  # 类型为 DataFeed
        # 缓存策略方法是否存在，避免重复 hasattr
        self._has_on_tick_cache: Dict[int, bool] = {}

    def run(self, *, cycles: int, max_seconds: float, interval: float) -> None:
        """启动主循环。"""
        cycles = max(1, int(cycles))
        interval = max(MIN_INTERVAL_SECONDS, float(interval))
        started_at = time.monotonic()

        self._strategy_manager.sync()
        loop_state = _LoopState(last_sync_time=time.monotonic())

        for cycle_index in range(cycles):
            now = time.monotonic()
            if max_seconds > 0 and (now - started_at) >= max_seconds:
                Logger.log("系统", "信息", f"达到最大运行时间 {max_seconds:.1f} 秒，循环终止")
                break

            # 获取系统信息
            account_info, terminal_info = self._fetch_system_info()
            if account_info is None or terminal_info is None:
                if self._handle_system_info_error(loop_state, interval):
                    break
                continue

            loop_state.consecutive_errors = 0

            # 风控检查
            if self._should_pause_for_account_guard(account_info, loop_state, interval, now):
                continue

            # 定期同步策略
            self._sync_if_needed(loop_state, now)

            # 获取启用策略列表
            enabled_strategies = [s for s in self._strategy_manager.active.values() if s.enabled]
            if not enabled_strategies:
                time.sleep(interval)
                continue

            # 批量获取订单、持仓和行情
            enabled_keys = {(s.magic, s.symbol) for s in enabled_strategies}
            orders_by_key, positions_by_key = self._fetch_order_position_maps(enabled_keys)
            ticks_by_symbol = self._fetch_ticks(enabled_strategies)

            # 执行每个策略
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
        """线程安全地获取账户和终端信息。"""
        with self._broker.lock:
            return self._broker.account_info(), self._broker.terminal_info()

    def _handle_system_info_error(self, loop_state: _LoopState, interval: float) -> bool:
        """处理系统信息获取失败，返回 True 表示需要终止循环。"""
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
        """风控检查：保证金比例过低时暂停所有策略。"""
        if account_info is None:
            loop_state.halted = False
            return False

        # 定期播报资金状况
        if now - loop_state.last_account_log_time > ACCOUNT_LOG_INTERVAL_SECONDS:
            Logger.log(
                "系统",
                "资金播报",
                (
                    f"余额: {account_info.balance:>10.2f} | 净值: {account_info.equity:>10.2f} | "
                    f"预付款: {account_info.margin:>10.2f} | 比例: {account_info.margin_level:>9.2f}%"
                ),
            )
            loop_state.last_account_log_time = now

        # 保证金比例过低则暂停
        if 0 < account_info.margin_level < 200:
            if not loop_state.halted:
                Logger.log("系统", "风控拦截", f"保证金比例极危 ({account_info.margin_level:>6.2f}%)，全策略暂停运行")
                loop_state.halted = True
            time.sleep(max(RETRY_SLEEP_SECONDS, interval))
            return True

        loop_state.halted = False
        return False

    def _sync_if_needed(self, loop_state: _LoopState, now: float) -> None:
        """定期同步策略配置。"""
        if now - loop_state.last_sync_time >= SYNC_INTERVAL_SECONDS:
            self._strategy_manager.sync()
            loop_state.last_sync_time = now

    def _fetch_order_position_maps(
        self,
        enabled_keys: Set[Tuple[int, str]],
    ) -> Tuple[Dict[Tuple[int, str], List[Any]], Dict[Tuple[int, str], List[Any]]]:
        """获取所有订单和持仓，并按 (magic, symbol) 分组。"""
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
        """将订单/持仓按 (magic, symbol) 分组，仅保留启用的策略键。"""
        if not items:
            return {}
        grouped = defaultdict(list)
        for item in items:
            key = (item.magic, item.symbol)
            if key in enabled_keys:
                grouped[key].append(item)
        return grouped

    def _fetch_ticks(self, strategies: List[Any]) -> Dict[str, Optional[mt5.Tick]]:
        """批量获取所有需要品种的最新报价。"""
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
        """执行单个策略的一次 tick 处理。"""
        actions = []
        try:
            ctx = self._build_strategy_context(
                strategy,
                magic,
                ticks_by_symbol,
                orders_by_key,
                positions_by_key,
            )

            # 根据策略接口调用相应方法（缓存 hasattr 结果）
            if self._has_on_tick(strategy):
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
            strategy._action_collector = None  # 清理引用

        self._flush_actions(strategy, actions)

    def _build_strategy_context(
        self,
        strategy: Any,
        magic: int,
        ticks_by_symbol: Dict[str, Optional[mt5.Tick]],
        orders_by_key: Dict[Tuple[int, str], List[Any]],
        positions_by_key: Dict[Tuple[int, str], List[Any]],
    ) -> StrategyContext:
        """构建策略上下文，包含 tick、订单、持仓和可选的 ATR。"""
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

    def _has_on_tick(self, strategy: Any) -> bool:
        """检查策略是否实现了 on_tick 方法（缓存结果）。"""
        strategy_id = id(strategy)  # 使用对象 id 作为缓存键，避免重复查找
        if strategy_id not in self._has_on_tick_cache:
            self._has_on_tick_cache[strategy_id] = hasattr(strategy, "on_tick")
        return self._has_on_tick_cache[strategy_id]

    def _flush_actions(self, strategy: Any, actions: List[Any]) -> None:
        """批量发送交易请求。"""
        if not actions:
            return

        for request in actions:
            if not isinstance(request, dict):
                continue

            action = request.get("action")
            if action in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING):
                result = strategy._send_with_fillings(request)  # 假设策略有该方法
            else:
                with self._broker.lock:
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