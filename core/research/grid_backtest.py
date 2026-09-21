"""基于闭合 M1/M15 K 线的纯网格事件回测器。

它刻意与 MT5、文件系统和策略配置隔离。调用方只需提供已校验的 M1 K 线、品种
规格及公开 profile，即可得到 OHLC/OLHC 两条保守路径中的较差结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import sqrt
from typing import Iterable, Mapping, Sequence

from core.domain import CommandAction, MarketSnapshot, RiskSettings, StrategySettings, SymbolSpec
from core.strategy.grid_v2 import GridPlanner, GridState, Inventory

from .history import HistoricalTick, InstrumentSpec
from .metrics import calculate_metrics
from .models import BacktestResult, Bar, Trade
from .profiles import BacktestProfile, CostProfile, CostScenario


UTC = timezone.utc


@dataclass(slots=True)
class _PendingLimit:
    key: str
    side: str
    volume: float
    price: float
    tp: float
    sl: float | None


@dataclass(slots=True)
class _Position:
    side: str
    volume: float
    entry_time: datetime
    raw_entry: float
    effective_entry: float
    tp: float
    sl: float | None
    entry_commission: float
    entry_slippage_cost: float


@dataclass(frozen=True, slots=True)
class GridBacktestRun:
    """单个成本情景、单个盘中路径的结果。"""

    path: str
    result: BacktestResult
    normalised_notional: float
    effective_costs: dict[str, float]


@dataclass(frozen=True, slots=True)
class ConservativeGridResult:
    """保守路径结果，同时保留另一条路径用于审计。"""

    conservative: GridBacktestRun
    alternatives: tuple[GridBacktestRun, ...]


@dataclass(frozen=True, slots=True)
class _SyntheticTick:
    bid: float
    ask: float


def _time(value: datetime | float) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("K 线时间必须带 UTC 时区")
        return value.astimezone(UTC)
    return datetime.fromtimestamp(float(value), tz=UTC)


def _aggregate_m15(bars: Sequence[Bar]) -> tuple[Bar, ...]:
    """只返回完整的 15 分钟桶，防止将未收盘数据送入指标。"""

    groups: list[list[Bar]] = []
    current_key: tuple[int, int, int, int, int] | None = None
    for bar in sorted(bars, key=lambda item: _time(item.timestamp)):
        stamp = _time(bar.timestamp)
        key = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute // 15)
        if key != current_key:
            groups.append([])
            current_key = key
        groups[-1].append(bar)
    result: list[Bar] = []
    for group in groups:
        first, last = group[0], group[-1]
        # 仅使用连续且恰好 15 根的完整分钟桶。
        if len(group) != 15:
            continue
        times = [_time(item.timestamp) for item in group]
        if any((right - left).total_seconds() != 60 for left, right in zip(times, times[1:])):
            continue
        result.append(
            Bar(
                timestamp=_time(last.timestamp),
                open=first.open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=last.close,
                spread=last.spread,
                volume=sum(item.volume for item in group),
            )
        )
    return tuple(result)


def _atr(bars: Sequence[Bar], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    tail = bars[-(period + 1) :]
    true_ranges = [
        max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
        for previous, current in zip(tail, tail[1:])
    ]
    return sum(true_ranges) / len(true_ranges)


def _ewma_daily_volatility(closes: Sequence[float], span: int, *, periods_per_year: int = 365) -> float | None:
    if len(closes) < 3:
        return None
    alpha = 2.0 / (span + 1.0)
    variance = 0.0
    initialized = False
    for prior, current in zip(closes, closes[1:]):
        ret = current / prior - 1.0
        if not initialized:
            variance = ret * ret
            initialized = True
        else:
            variance = alpha * ret * ret + (1.0 - alpha) * variance
    return sqrt(max(variance, 0.0)) * sqrt(periods_per_year)


class GridBacktestAdapter:
    """把纯 ``GridPlanner`` 接入具有成交、保护价与成本的历史事件循环。"""

    def __init__(
        self,
        profile: BacktestProfile,
        instrument: InstrumentSpec,
        cost: CostProfile,
        *,
        strategy_magic: int = 900_001,
    ) -> None:
        self.profile = profile
        self.instrument = instrument
        self.cost = cost
        self.strategy_magic = strategy_magic
        self._planner = GridPlanner()
        self._symbol = SymbolSpec(
            symbol=instrument.symbol,
            price_tick=instrument.tick_size,
            volume_min=instrument.volume_min,
            volume_max=instrument.volume_max,
            volume_step=instrument.volume_step,
            contract_size=instrument.contract_size,
        )
        self._periods_per_year = 365 if "BTC" in instrument.symbol.upper() else 252

    def run(
        self,
        bars: Sequence[Bar],
        *,
        scenario: CostScenario = CostScenario.BASELINE,
        tick_overrides: Mapping[datetime, Sequence[HistoricalTick]] | None = None,
    ) -> ConservativeGridResult:
        """在 OHLC 与 OLHC 两种可解释路径分别运行，返回权益较低的一条。"""

        prepared = tuple(
            item
            for item in sorted(bars, key=lambda value: _time(value.timestamp))
            if self.profile.start <= _time(item.timestamp) <= self.profile.end
        )
        if not prepared:
            raise ValueError("回测区间没有可用 M1 K 线")
        runs = tuple(self._run_path(prepared, path, scenario, tick_overrides or {}) for path in ("OHLC", "OLHC"))
        conservative = min(runs, key=lambda item: item.result.equity_curve[-1])
        return ConservativeGridResult(
            conservative=conservative,
            alternatives=tuple(item for item in runs if item is not conservative),
        )

    def _run_path(
        self,
        bars: Sequence[Bar],
        path: str,
        scenario: CostScenario,
        tick_overrides: Mapping[datetime, Sequence[HistoricalTick]],
    ) -> GridBacktestRun:
        pending: dict[str, _PendingLimit] = {}
        positions: list[_Position] = []
        trades: list[Trade] = []
        equity_curve: list[float] = [self.profile.initial_equity]
        daily_equity: list[float] = [self.profile.initial_equity]
        cash = self.profile.initial_equity
        grid_state = GridState()
        closed_m15: list[Bar] = []
        day_closes: list[float] = []
        cached_volatility: float | None = None
        cached_volatility_days = -1
        current_day = _time(bars[0].timestamp).date()
        current_m15: list[Bar] = []
        tick_replayed_bars = 0
        costs = {"commission": 0.0, "slippage": 0.0, "swap": 0.0}
        maximum_notional = 0.0

        for index, bar in enumerate(bars):
            now = _time(bar.timestamp)
            if now.date() != current_day:
                cash = self._apply_swap(cash, positions, now, scenario, costs, trades)
                daily_equity.append(equity_curve[-1])
                day_closes.append(bars[index - 1].close)
                current_day = now.date()

            minute = now.replace(second=0, microsecond=0)
            tick_path = tick_overrides.get(minute, ())
            if tick_path:
                cash = self._simulate_ticks(cash, bar, tick_path, pending, positions, trades, scenario, costs)
                tick_replayed_bars += 1
            else:
                cash = self._simulate_bar(cash, bar, path, pending, positions, trades, scenario, costs)
            cash = self._enforce_gross_budget(cash, bar, positions, trades, scenario, costs)
            current_equity = self._marked_equity(cash, positions, bar.close, self._spread_price(bar))
            equity_curve.append(current_equity)
            maximum_notional = max(maximum_notional, self._gross_notional(positions, bar.close))

            # 收到本分钟的收盘价后才可能关闭 M15 桶；随后生成的目标订单从下一根 M1 起生效。
            current_m15.append(bar)
            if now.minute % 15 == 14:
                if self._is_complete_m15(current_m15):
                    first, last = current_m15[0], current_m15[-1]
                    closed_m15.append(
                        Bar(
                            timestamp=last.timestamp,
                            open=first.open,
                            high=max(item.high for item in current_m15),
                            low=min(item.low for item in current_m15),
                            close=last.close,
                            spread=last.spread,
                            volume=sum(item.volume for item in current_m15),
                        )
                    )
                    atr = _atr(closed_m15, self.profile.atr_period)
                    if atr is not None:
                        # 日收盘序列在日内不变，避免每个 M15 桶重复扫描全部历史日线。
                        if cached_volatility_days != len(day_closes):
                            cached_volatility = _ewma_daily_volatility(
                                day_closes,
                                self.profile.ewma_span_days,
                                periods_per_year=self._periods_per_year,
                            )
                            cached_volatility_days = len(day_closes)
                        vol = cached_volatility
                        target_ratio = self.profile.max_gross_notional_equity_ratio
                        if vol is not None and vol > 1e-12:
                            target_ratio = min(target_ratio, self.profile.target_annual_volatility / vol)
                        target_notional = max(0.0, current_equity * target_ratio)
                        volume = target_notional / self.profile.target_orders / max(
                            bar.close * self.instrument.contract_size, 1e-12
                        )
                        max_net_volume = (
                            target_notional
                            * self.profile.max_net_notional_gross_ratio
                            / max(bar.close * self.instrument.contract_size, 1e-12)
                        )
                        settings = StrategySettings(
                            symbol=self.instrument.symbol,
                            magic=self.strategy_magic,
                            step=atr * self.profile.grid_step_atr,
                            tp_dist=atr * self.profile.take_profit_atr,
                            lot=volume,
                            legacy={
                                "mode": "neutral",
                                "window": self.profile.grid_levels_per_side,
                                "sl_dist": atr * self.profile.stop_loss_atr,
                                "hedge_fraction": 1.0,
                            },
                            risk=RiskSettings(
                                max_net_volume=max_net_volume,
                                max_open_orders=self.profile.max_open_orders,
                            ),
                        )
                        inventory = self._inventory(positions, pending.values())
                        spread = self._spread_price(bar)
                        decision, grid_state = self._planner.plan(
                            settings=settings,
                            symbol=self._symbol,
                            snapshot=MarketSnapshot(
                                symbol=self.instrument.symbol,
                                tick=_SyntheticTick(bid=bar.close, ask=bar.close + spread),
                                orders=[],
                                positions=[],
                                now=now.timestamp(),
                                atr=atr,
                            ),
                            state=grid_state,
                            inventory=inventory,
                        )
                        cash = self._reconcile(
                            cash,
                            decision.commands,
                            pending,
                            positions,
                            bar.close,
                            current_equity,
                            now,
                            scenario,
                            costs,
                        )
                current_m15 = []

        # 统一按最后一个可见报价平仓，保证收益、交易明细与成本指标口径一致。
        final_bar = bars[-1]
        final_spread = self._spread_price(final_bar)
        for position in tuple(positions):
            raw_exit = final_bar.close if position.side == "buy" else final_bar.close + final_spread
            cash = self._close_position(cash, position, raw_exit, final_bar, positions, trades, scenario, costs)
        final_equity = cash
        equity_curve[-1] = final_equity
        daily_equity.append(final_equity)
        metrics = calculate_metrics(daily_equity, trades, periods_per_year=self._periods_per_year)
        result = BacktestResult(
            equity_curve=tuple(daily_equity),
            trades=tuple(trades),
            metrics=metrics,
            diagnostics={
                "intrabar_path": path,
                "open_positions": len(positions),
                "pending_orders": len(pending),
                "gross_notional_peak": maximum_notional,
                "costs": dict(costs),
                "m15_closed_bars": len(closed_m15),
                "tick_replayed_bars": tick_replayed_bars,
            },
        )
        return GridBacktestRun(path=path, result=result, normalised_notional=maximum_notional, effective_costs=costs)

    def _simulate_bar(
        self,
        cash: float,
        bar: Bar,
        path: str,
        pending: dict[str, _PendingLimit],
        positions: list[_Position],
        trades: list[Trade],
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        values = (bar.open, bar.high, bar.low, bar.close) if path == "OHLC" else (bar.open, bar.low, bar.high, bar.close)
        spread = self._spread_price(bar)
        for start, end in zip(values, values[1:]):
            cash = self._simulate_segment(
                cash, start, end, spread, bar, pending, positions, trades, scenario, costs
            )
        return cash

    def _simulate_ticks(
        self,
        cash: float,
        bar: Bar,
        ticks: Sequence[HistoricalTick],
        pending: dict[str, _PendingLimit],
        positions: list[_Position],
        trades: list[Trade],
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        """仅在已选极端窗口使用真实 tick 顺序替换 OHLC/OLHC 假设。"""

        valid = [item for item in sorted(ticks, key=lambda item: item.timestamp) if item.bid > 0]
        if not valid:
            return self._simulate_bar(cash, bar, "OHLC", pending, positions, trades, scenario, costs)
        previous_bid = valid[0].bid
        previous_spread = max(0.0, valid[0].ask - valid[0].bid) if valid[0].ask > 0 else self._spread_price(bar)
        # 首笔报价本身也可以触发已挂出的限价单。
        cash = self._simulate_segment(
            cash,
            previous_bid,
            previous_bid,
            previous_spread,
            bar,
            pending,
            positions,
            trades,
            scenario,
            costs,
        )
        for tick in valid[1:]:
            spread = max(0.0, tick.ask - tick.bid) if tick.ask > 0 else previous_spread
            cash = self._simulate_segment(
                cash,
                previous_bid,
                tick.bid,
                spread,
                bar,
                pending,
                positions,
                trades,
                scenario,
                costs,
            )
            previous_bid, previous_spread = tick.bid, spread
        return cash

    def _simulate_segment(
        self,
        cash: float,
        start: float,
        end: float,
        spread: float,
        bar: Bar,
        pending: dict[str, _PendingLimit],
        positions: list[_Position],
        trades: list[Trade],
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        cash = self._close_protection(cash, start, end, spread, bar, positions, trades, scenario, costs)
        for key, order in tuple(pending.items()):
            quote_start = start + spread if order.side == "buy" else start
            quote_end = end + spread if order.side == "buy" else end
            if min(quote_start, quote_end) <= order.price <= max(quote_start, quote_end):
                filled = order.volume * self.profile.partial_fill_ratio
                if filled <= 0:
                    continue
                cash_delta, position = self._open_position(order, filled, bar, scenario, costs)
                cash += cash_delta
                positions.append(position)
                order.volume -= filled
                if order.volume <= self.instrument.volume_step * 0.25:
                    pending.pop(key, None)
                # 限价单在此段触发后，仅用余下路径检查保护价。
                cash = self._close_protection(
                    cash,
                    order.price - spread if order.side == "buy" else order.price,
                    end,
                    spread,
                    bar,
                    positions,
                    trades,
                    scenario,
                    costs,
                )
        return cash

    def _close_protection(
        self,
        cash: float,
        start_bid: float,
        end_bid: float,
        spread: float,
        bar: Bar,
        positions: list[_Position],
        trades: list[Trade],
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        for position in tuple(positions):
            start = start_bid if position.side == "buy" else start_bid + spread
            end = end_bid if position.side == "buy" else end_bid + spread
            target: float | None = None
            if min(start, end) <= position.tp <= max(start, end):
                target = position.tp
            if position.sl is not None and min(start, end) <= position.sl <= max(start, end):
                # 同一单调线段理论上不会同时击穿 TP/SL；若数据异常，则保守地优先止损。
                target = position.sl
            if target is not None:
                cash = self._close_position(cash, position, target, bar, positions, trades, scenario, costs)
        return cash

    def _open_position(
        self,
        order: _PendingLimit,
        volume: float,
        bar: Bar,
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> tuple[float, _Position]:
        raw = order.price
        slip = self.cost.slippage(raw, scenario)
        effective = raw + slip if order.side == "buy" else raw - slip
        commission = self.cost.commission(effective, volume, self.instrument.contract_size)
        slip_cost = abs(self.instrument.pnl(raw, effective, volume, order.side))
        costs["commission"] += commission
        costs["slippage"] += slip_cost
        return -commission, _Position(
            side=order.side,
            volume=volume,
            entry_time=_time(bar.timestamp),
            raw_entry=raw,
            effective_entry=effective,
            tp=order.tp,
            sl=order.sl,
            entry_commission=commission,
            entry_slippage_cost=slip_cost,
        )

    def _close_position(
        self,
        cash: float,
        position: _Position,
        raw_exit: float,
        bar: Bar,
        positions: list[_Position],
        trades: list[Trade],
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        slip = self.cost.slippage(raw_exit, scenario)
        effective_exit = raw_exit - slip if position.side == "buy" else raw_exit + slip
        commission = self.cost.commission(effective_exit, position.volume, self.instrument.contract_size)
        slip_cost = abs(self.instrument.pnl(raw_exit, effective_exit, position.volume, position.side))
        gross = self.instrument.pnl(position.raw_entry, raw_exit, position.volume, position.side)
        total_cost = position.entry_commission + commission + position.entry_slippage_cost + slip_cost
        realised = self.instrument.pnl(position.effective_entry, effective_exit, position.volume, position.side) - commission
        costs["commission"] += commission
        costs["slippage"] += slip_cost
        trades.append(
            Trade(
                entry_time=position.entry_time,
                exit_time=_time(bar.timestamp),
                side=position.side,
                volume=position.volume,
                entry_price=position.raw_entry,
                exit_price=raw_exit,
                gross_pnl=gross,
                cost=total_cost,
            )
        )
        positions.remove(position)
        return cash + realised

    def _reconcile(
        self,
        cash: float,
        commands: Iterable,
        pending: dict[str, _PendingLimit],
        positions: list[_Position],
        price: float,
        equity: float,
        now: datetime,
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        command_list = tuple(commands)
        desired = {
            command.idempotency_key
            for command in command_list
            if command.action is CommandAction.PLACE_LIMIT
        }
        for key in tuple(pending):
            if key not in desired:
                pending.pop(key, None)

        # 持仓在本轮协调中不变，挂单名义金额随新增指令增量累加。
        projected_notional = self._gross_notional(positions, price)
        projected_notional += self._pending_notional(pending.values(), price)
        notional_cap = equity * self.profile.max_gross_notional_equity_ratio

        for command in command_list:
            payload = command.payload
            if command.action is CommandAction.PLACE_LIMIT and command.idempotency_key not in pending:
                if len(pending) >= self.profile.max_open_orders:
                    continue
                volume = float(payload["volume"])
                order_notional = price * volume * self.instrument.contract_size
                if projected_notional + order_notional > notional_cap:
                    continue
                pending[command.idempotency_key] = _PendingLimit(
                    key=command.idempotency_key,
                    side=str(payload["side"]),
                    volume=volume,
                    price=self._symbol.ticks_to_price(int(payload["price_ticks"])),
                    tp=self._symbol.ticks_to_price(int(payload["tp_ticks"])),
                    sl=self._symbol.ticks_to_price(int(payload["sl_ticks"])) if "sl_ticks" in payload else None,
                )
                projected_notional += order_notional
            elif command.action is CommandAction.OPEN_HEDGE:
                volume = float(payload["volume"])
                side = str(payload["side"])
                # 对冲按下一次可见收盘价成交，绝不引用未来报价。
                order = _PendingLimit(command.idempotency_key, side, volume, price, 0.0, None)
                cash_delta, position = self._open_position(order, volume, Bar(now, price, price, price, price), scenario, costs)
                # 对冲不设固定 TP/SL，仍受到总敞口预算与标记风险约束。
                position.tp = float("inf") if side == "buy" else float("-inf")
                positions.append(position)
                cash += cash_delta
        return cash

    def _apply_swap(
        self,
        cash: float,
        positions: Sequence[_Position],
        now: datetime,
        scenario: CostScenario,
        costs: dict[str, float],
        trades: list[Trade],
    ) -> float:
        multiplier = 3.0 if self.instrument.rollover3_weekday == now.weekday() else 1.0
        for position in positions:
            value: float | None = None
            swap = self.instrument.swap_long if position.side == "buy" else self.instrument.swap_short
            if swap is not None and self.instrument.swap_mode in {"points", "currency_deposit"}:
                if self.instrument.swap_mode == "points":
                    value = swap * self.instrument.point / self.instrument.tick_size * self.instrument.tick_value * position.volume
                else:
                    # 只有存款货币模式可直接作为 USD 记账；其他币种模式不能伪造换汇率。
                    value = swap * position.volume
            if value is None:
                funding_bps = self.cost.fallback_funding_bps(scenario)
                if funding_bps is None:
                    continue
                value = -position.effective_entry * position.volume * self.instrument.contract_size * funding_bps / 10_000.0
            signed_value = value * multiplier
            charge = max(0.0, -signed_value)
            costs["swap"] += charge
            cash += signed_value
            trades.append(
                Trade(
                    entry_time=now,
                    exit_time=now,
                    side="swap",
                    volume=0.0,
                    entry_price=position.effective_entry,
                    exit_price=position.effective_entry,
                    gross_pnl=max(0.0, signed_value),
                    cost=charge,
                )
            )
        return cash

    def _enforce_gross_budget(
        self,
        cash: float,
        bar: Bar,
        positions: list[_Position],
        trades: list[Trade],
        scenario: CostScenario,
        costs: dict[str, float],
    ) -> float:
        """价格跳变使已有仓位突破总名义预算时，先减仓再允许下一轮规划。"""

        spread = self._spread_price(bar)
        equity = self._marked_equity(cash, positions, bar.close, spread)
        cap = max(0.0, equity * self.profile.max_gross_notional_equity_ratio)
        while positions and self._gross_notional(positions, bar.close) > cap:
            # 优先移除名义金额最大的仓位，避免在极端行情中继续累积总风险。
            position = max(positions, key=lambda item: item.volume)
            raw_exit = bar.close if position.side == "buy" else bar.close + spread
            cash = self._close_position(cash, position, raw_exit, bar, positions, trades, scenario, costs)
            equity = self._marked_equity(cash, positions, bar.close, spread)
            cap = max(0.0, equity * self.profile.max_gross_notional_equity_ratio)
        return cash

    def _spread_price(self, bar: Bar) -> float:
        return max(0.0, float(bar.spread) * self.instrument.point)

    def _marked_equity(self, cash: float, positions: Iterable[_Position], bid: float, spread: float) -> float:
        return cash + sum(
            self.instrument.pnl(
                position.effective_entry,
                bid if position.side == "buy" else bid + spread,
                position.volume,
                position.side,
            )
            for position in positions
        )

    def _inventory(self, positions: Iterable[_Position], pending: Iterable[_PendingLimit]) -> Inventory:
        values = tuple(positions)
        orders = tuple(pending)
        return Inventory(
            long_volume=sum(item.volume for item in values if item.side == "buy"),
            short_volume=sum(item.volume for item in values if item.side == "sell"),
            pending_buy_volume=sum(item.volume for item in orders if item.side == "buy"),
            pending_sell_volume=sum(item.volume for item in orders if item.side == "sell"),
        )

    def _gross_notional(self, positions: Iterable[_Position], price: float) -> float:
        return sum(item.volume for item in positions) * price * self.instrument.contract_size

    def _pending_notional(self, orders: Iterable[_PendingLimit], price: float) -> float:
        return sum(item.volume for item in orders) * price * self.instrument.contract_size

    @staticmethod
    def _is_complete_m15(group: Sequence[Bar]) -> bool:
        if len(group) != 15:
            return False
        times = [_time(item.timestamp) for item in group]
        return all((right - left).total_seconds() == 60 for left, right in zip(times, times[1:]))
