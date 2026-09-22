from __future__ import annotations

from datetime import datetime, timezone
import unittest

from core.domain import CommandAction, OrderCommand
from core.research import (
    BacktestProfile,
    CostScenario,
    GridBacktestAdapter,
    InstrumentSpec,
    XAUUSD_STANDARD_COST,
)
from inv.backtest.engine import BacktestEngine
from inv.backtest.optimizer import ParameterGrid, ParameterOptimizer, WalkForwardOptimizer
from inv.domain.models import Bar, Signal, SignalType, StageInput
from inv.domain.settings import StrategySettings
from inv.pipeline.runner import PipelineRunner
from inv.strategy.breakout import BreakoutStrategy
from inv.strategy.grid_planner import GridPlanner
from inv.strategy.mean_reversion import MeanReversionStrategy
from inv.strategy.momentum import MomentumStrategy
from inv.strategy.base import BaseStrategy


class _TrackingBars(list):
    def __init__(self, values):
        super().__init__(values)
        self.slices: list[slice] = []

    def __getitem__(self, item):
        if isinstance(item, slice):
            self.slices.append(item)
        return super().__getitem__(item)


def _bars(count: int) -> list[Bar]:
    return [
        Bar(
            timestamp=float(index),
            open=100.0 + index * 0.01,
            high=100.2 + index * 0.01,
            low=99.8 + index * 0.01,
            close=100.0 + index * 0.01,
        )
        for index in range(count)
    ]


class StrategyOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = StrategySettings(
            symbol="TEST",
            magic=7,
            min_price=90.0,
            max_price=120.0,
        )

    def test_strategies_only_slice_bounded_history(self) -> None:
        cases = (
            (MomentumStrategy("m", "TEST"), 80),
            (MeanReversionStrategy("r", "TEST"), 40),
            (BreakoutStrategy("b", "TEST"), 80),
            (GridPlanner("g", "TEST"), 40),
        )
        plain = _bars(1_000)
        for strategy, maximum_window in cases:
            with self.subTest(strategy=type(strategy).__name__):
                tracked = _TrackingBars(plain)
                actual = strategy.compute_signals(tracked, 999, self.settings)
                expected = type(strategy)(strategy.strategy_id, strategy.symbol).compute_signals(
                    plain, 999, self.settings
                )
                self.assertEqual(actual, expected)
                for requested in tracked.slices:
                    start = 0 if requested.start is None else requested.start
                    stop = len(tracked) if requested.stop is None else requested.stop
                    self.assertLessEqual(stop - start, maximum_window)

    def test_empty_history_returns_hold_signal(self) -> None:
        signal = MomentumStrategy("m", "TEST").compute_signals([], 0, self.settings)[0]
        self.assertEqual(signal.signal_type, SignalType.HOLD)
        self.assertEqual(signal.metadata["strategy"], "momentum")

    def test_shared_atr_keeps_the_last_period_semantics(self) -> None:
        bars = [
            Bar(float(index), 10.0, 10.0 + index, 9.0 - index, 9.5 + index)
            for index in range(6)
        ]
        period = 3
        expected_ranges = []
        tail = bars[1:6]
        for previous, current in zip(tail, tail[1:]):
            expected_ranges.append(max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            ))
        expected = sum(expected_ranges[-period:]) / period
        self.assertEqual(
            MomentumStrategy._calc_atr(bars, current_index=5, period=period),
            expected,
        )

    def test_optimizer_builds_valid_default_settings(self) -> None:
        optimizer = ParameterOptimizer(
            BacktestEngine(),
            lambda params: MomentumStrategy("m", "TEST"),
            ParameterGrid({}),
        )
        results = optimizer.run(_bars(40))
        self.assertEqual(len(results), 1)

    def test_walk_forward_rejects_non_positive_sizes(self) -> None:
        optimizer = ParameterOptimizer(
            BacktestEngine(),
            lambda params: MomentumStrategy("m", "TEST"),
            ParameterGrid({}),
        )
        with self.assertRaisesRegex(ValueError, "必须为正整数"):
            WalkForwardOptimizer(optimizer, step_size=0)

    def test_pipeline_rejects_empty_stage_list(self) -> None:
        stage_input = StageInput(
            symbol="TEST",
            bars=(),
            start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            PipelineRunner().run(stage_input)

    def test_active_close_immediately_resets_strategy_position(self) -> None:
        class StateMachineStrategy(BaseStrategy):
            def __init__(self):
                super().__init__("state", "TEST")
                self.positions_seen: list[float] = []

            def compute_signals(self, bars, current_index, settings):
                self.positions_seen.append(self.state.position)
                signal_type = {
                    0: SignalType.BUY,
                    1: SignalType.CLOSE_LONG,
                    2: SignalType.BUY,
                }.get(current_index, SignalType.HOLD)
                return [
                    Signal(
                        timestamp=self._get_timestamp(bars, current_index),
                        symbol=self.symbol,
                        signal_type=signal_type,
                        price=float(bars[current_index].close),
                        volume=1.0,
                    )
                ]

        strategy = StateMachineStrategy()
        BacktestEngine(initial_equity=100_000.0, commission_pct=0.0, slippage_pct=0.0).run(
            strategy,
            _bars(5),
            self.settings,
        )
        self.assertEqual(strategy.positions_seen[:4], [0.0, 1.0, 0.0, 1.0])


class GridReconcileOptimizationTests(unittest.TestCase):
    def test_reconcile_accepts_generator_and_aggregates_notional_once(self) -> None:
        spec = InstrumentSpec(
            symbol="TEST",
            point=0.01,
            digits=2,
            contract_size=1.0,
            tick_size=0.01,
            tick_value=1.0,
            volume_min=0.01,
            volume_max=10.0,
            volume_step=0.01,
        )
        profile = BacktestProfile(
            end=datetime(2024, 1, 2, tzinfo=timezone.utc),
            max_open_orders=10,
            max_gross_notional_equity_ratio=1.0,
        )
        adapter = GridBacktestAdapter(profile, spec, XAUUSD_STANDARD_COST)
        calls = {"positions": 0, "pending": 0}
        original_positions = adapter._gross_notional
        original_pending = adapter._pending_notional

        def gross(positions, price):
            calls["positions"] += 1
            return original_positions(positions, price)

        def pending(orders, price):
            calls["pending"] += 1
            return original_pending(orders, price)

        adapter._gross_notional = gross
        adapter._pending_notional = pending
        commands = (
            OrderCommand(
                idempotency_key=f"order-{index}",
                action=CommandAction.PLACE_LIMIT,
                strategy_id="grid:TEST",
                symbol="TEST",
                payload={
                    "side": "buy",
                    "volume": 1.0,
                    "price_ticks": 10_000 - index,
                    "tp_ticks": 10_100,
                },
            )
            for index in range(3)
        )
        pending_orders = {}
        adapter._reconcile(
            100_000.0,
            commands,
            pending_orders,
            [],
            100.0,
            100_000.0,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            CostScenario.BASELINE,
            {"commission": 0.0, "slippage": 0.0, "swap": 0.0},
        )
        self.assertEqual(len(pending_orders), 3)
        self.assertEqual(calls, {"positions": 1, "pending": 1})


if __name__ == "__main__":
    unittest.main()
