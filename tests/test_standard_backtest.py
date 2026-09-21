from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from core.research import (
    BacktestProfile,
    Bar,
    CostScenario,
    GridBacktestAdapter,
    InstrumentSpec,
    HistoryUnavailableError,
    Mt5HistoryProvider,
    StandardComparisonRunner,
    XAUUSD_STANDARD_COST,
    assess_tick_coverage,
    select_extreme_windows,
    validate_m1_bars,
)
from core.research.symbols import resolve_broker_symbol
from core.research.comparison import InsufficientHistoryError
from core.research.reporting import ReviewWindow
from inv.backtest.engine import BacktestEngine, Position, _ExecutionState
from inv.backtest.optimizer import ParameterGrid


UTC = timezone.utc


def make_bar(index: int, *, low: float = 99.9, high: float = 100.1, close: float = 100.0) -> Bar:
    return Bar(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        open=100.0,
        high=high,
        low=low,
        close=close,
        spread=2.0,
        volume=100.0,
    )


class _FakeHistoryBroker:
    def copy_rates_range(self, symbol, timeframe, start, end):
        return [
            {
                "time": 1704067200,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "spread": 2,
                "tick_volume": 12,
            }
        ]

    def copy_ticks_range(self, symbol, start, end):
        return [{"time_msc": 1704067200000, "bid": 100.0, "ask": 100.1, "volume": 1.0}]

    def symbol_info(self, symbol):
        return SimpleNamespace(
            point=0.01,
            digits=2,
            trade_contract_size=100.0,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            swap_long=-10.0,
            swap_short=-8.0,
            swap_mode=4,
            swap_rollover3days=2,
        )


class StandardBacktestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = InstrumentSpec(
            symbol="XAUUSD",
            point=0.01,
            digits=2,
            contract_size=100.0,
            tick_size=0.01,
            tick_value=1.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )

    def test_mt5_history_adapter_converts_range_without_mt5_import(self) -> None:
        provider = Mt5HistoryProvider(_FakeHistoryBroker())
        start = datetime(2024, 1, 1, tzinfo=UTC)
        bars = provider.bars("XAUUSD", start, start + timedelta(minutes=1))
        ticks = provider.ticks("XAUUSD", start, start + timedelta(minutes=1))
        spec = provider.specification("XAUUSD")
        self.assertEqual(bars[0].timestamp, start)
        self.assertEqual(bars[0].spread, 2.0)
        self.assertEqual(ticks[0].ask, 100.1)
        self.assertEqual(spec.swap_mode, "currency_deposit")

    def test_validation_reports_duplicates_and_critical_gap(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        bars = [
            Bar(start, 1.0, 1.1, 0.9, 1.0),
            Bar(start, 1.0, 1.1, 0.9, 1.0),
            Bar(start + timedelta(minutes=10), 1.0, 1.1, 0.9, 1.0),
        ]
        quality = validate_m1_bars("BTCUSD", bars, start, start + timedelta(minutes=10), trades_24_7=True)
        self.assertEqual(quality.duplicate_bars, 1)
        self.assertEqual(quality.gap_count, 1)

    def test_grid_orders_are_created_only_after_closed_m15_atr(self) -> None:
        # 第 225 分钟的异常低点可改变 ATR，但此时订单刚产生，不能在同一根 K 线上成交。
        bars = [make_bar(index) for index in range(224)]
        bars.append(make_bar(224, low=90.0))
        bars.append(make_bar(225))
        profile = BacktestProfile(end=datetime(2024, 1, 2, tzinfo=UTC))
        result = GridBacktestAdapter(profile, self.spec, XAUUSD_STANDARD_COST).run(bars)
        self.assertEqual(result.conservative.result.metrics.trade_count, 0)
        self.assertEqual(result.conservative.result.diagnostics["m15_closed_bars"], 15)

    def test_tick_status_never_claims_validation_from_missing_ticks(self) -> None:
        window = ReviewWindow(
            "最大回撤",
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 8, tzinfo=UTC),
            0.2,
        )
        status = assess_tick_coverage((window,), ())
        self.assertFalse(status.tick_validated)
        self.assertIn("M1", status.status)

    def test_cost_profile_uses_configured_stress_slippage(self) -> None:
        self.assertGreater(
            XAUUSD_STANDARD_COST.slippage(2000.0, CostScenario.STRESS),
            XAUUSD_STANDARD_COST.slippage(2000.0, CostScenario.BASELINE),
        )

    def test_symbol_resolver_prefers_mt5_broker_suffix_name(self) -> None:
        available = ("BTCUSDc", "BTCUSDTc", "XAUUSDc", "XAUUSD")
        self.assertEqual(resolve_broker_symbol("XAUUSD", available, preferred="XAUUSDc"), "XAUUSDc")
        self.assertEqual(resolve_broker_symbol("BTCUSD", available, preferred="BTCUSDc"), "BTCUSDc")

    def test_history_provider_queries_long_range_in_chunks_and_deduplicates_edges(self) -> None:
        calls = []

        class Broker:
            def copy_rates_range(self, symbol, timeframe, start, end):
                calls.append((start, end))
                return [
                    {"time": start.timestamp(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
                    {"time": end.timestamp(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
                ]

        start = datetime(2024, 1, 1, tzinfo=UTC)
        bars = Mt5HistoryProvider(Broker(), rate_chunk_days=1).bars("XAUUSDc", start, start + timedelta(days=3))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(bars), 4)

    def test_history_provider_explains_terminal_query_failure(self) -> None:
        class Broker:
            def copy_rates_range(self, symbol, timeframe, start, end):
                return None

            def last_error(self):
                return (-1, "Terminal: Call failed")

        with self.assertRaises(HistoryUnavailableError) as raised:
            Mt5HistoryProvider(Broker()).bars(
                "XAUUSDc",
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 2, 1, tzinfo=UTC),
            )
        self.assertIn("Terminal: Call failed", str(raised.exception))

    def test_history_provider_falls_back_to_start_time_query(self) -> None:
        class Broker:
            def copy_rates_range(self, symbol, timeframe, start, end):
                return None

            def copy_rates_from(self, symbol, timeframe, start, count):
                return [{"time": start.timestamp(), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}]

        start = datetime(2024, 1, 1, tzinfo=UTC)
        bars = Mt5HistoryProvider(Broker()).bars("XAUUSDc", start, start + timedelta(days=1))
        self.assertEqual(len(bars), 1)

    def test_incomplete_history_refuses_formal_comparison(self) -> None:
        class Provider:
            def specification(self, symbol):
                return self_spec

            def bars(self, symbol, start, end, timeframe="M1"):
                return (make_bar(0), make_bar(1))

            def ticks(self, symbol, start, end):
                return ()

        self_spec = self.spec
        with self.assertRaises(InsufficientHistoryError):
            StandardComparisonRunner(Provider()).run(request_tick_review=False)

    def test_backtest_mark_to_market_matches_cash_accounting(self) -> None:
        engine = BacktestEngine()
        long_state = _ExecutionState(
            cash=90_000.0,
            position=Position("long", 1.0, 100.0, 0.0),
        )
        short_state = _ExecutionState(
            cash=110_000.0,
            position=Position("short", 1.0, 100.0, 0.0),
        )
        self.assertEqual(engine._mark_to_market(long_state, 110.0, 100.0), 101_000.0)
        self.assertEqual(engine._mark_to_market(short_state, 90.0, 100.0), 101_000.0)

    def test_trade_cost_includes_entry_and_exit_costs(self) -> None:
        from inv.domain.models import Signal, SignalType

        engine = BacktestEngine(commission_pct=0.01, slippage_pct=0.01)
        state = _ExecutionState(cash=100_000.0)
        trades = []
        signal = Signal(
            datetime(2024, 1, 1, tzinfo=UTC), "TEST", SignalType.BUY,
            price=100.0, volume=1.0,
        )
        engine._execute_signal(state, signal, 100.0, 1.0, trades, 1.0)
        engine._force_close(state, 110.0, 2.0, trades, 1.0)
        self.assertAlmostEqual(trades[0].cost, 4.2)

    def test_random_parameter_sample_does_not_materialize_full_grid(self) -> None:
        grid = ParameterGrid({"a": list(range(10_000)), "b": list(range(10_000))})
        grid.all_params = lambda: self.fail("随机采样不应物化完整笛卡尔积")
        sampled = grid.sample(20, seed=7)
        self.assertEqual(len(sampled), 20)
        self.assertEqual(len({(item["a"], item["b"]) for item in sampled}), 20)


if __name__ == "__main__":
    unittest.main()
