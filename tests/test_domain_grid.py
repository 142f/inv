from __future__ import annotations

from types import SimpleNamespace
import unittest

from core.domain import MarketSnapshot, StrategySettings, SymbolSpec
from core.strategy.grid_v2 import GridPlanner, GridState, HedgePlanner, Inventory


class GridPlannerTests(unittest.TestCase):
    def test_volume_and_price_are_normalized_by_rules(self) -> None:
        spec = SymbolSpec("TEST", price_tick=0.01, volume_min=0.1, volume_max=2.0, volume_step=0.1)
        self.assertEqual(spec.price_to_ticks(1.23), 123)
        self.assertEqual(spec.price_to_ticks(1.005), 101)
        self.assertAlmostEqual(spec.ticks_to_price(123), 1.23)
        self.assertAlmostEqual(spec.normalize_volume(0.29), 0.2)
        self.assertEqual(spec.normalize_volume(0.01), 0.0)

    def test_planner_only_returns_pure_order_commands(self) -> None:
        raw = {
            "symbol": "TEST",
            "magic": 7,
            "step": 0.1,
            "tp_dist": 0.05,
            "lot": 0.2,
            "min_p": 9.0,
            "max_p": 11.0,
            "window": 2,
            "max_net_vol": 1.0,
            "enabled": True,
        }
        settings = StrategySettings.from_legacy(raw)
        snapshot = MarketSnapshot(
            symbol="TEST",
            tick=SimpleNamespace(bid=10.00, ask=10.02),
            orders=[],
            positions=[],
            now=1.0,
            atr=0.1,
        )
        decision, state = GridPlanner().plan(
            settings=settings,
            symbol=SymbolSpec("TEST", 0.01, 0.1, 10.0, 0.1),
            snapshot=snapshot,
            state=GridState(),
            inventory=Inventory(0.0, 0.0, 0.0, 0.0),
        )
        self.assertEqual(state.anchor_ticks, 1001)
        self.assertGreaterEqual(len(decision.commands), 2)
        self.assertTrue(all(command.strategy_id == "7:TEST" for command in decision.commands))
        self.assertTrue(all("price_ticks" in command.payload for command in decision.commands))

    def test_hedge_planner_is_directionally_symmetric(self) -> None:
        long_hedge = HedgePlanner.plan(
            strategy_id="7:TEST",
            symbol="TEST",
            inventory=Inventory(2.0, 0.0, 0.0, 0.0),
            max_net_volume=1.0,
            fraction=1.0,
            volume_step=0.1,
        )
        short_hedge = HedgePlanner.plan(
            strategy_id="7:TEST",
            symbol="TEST",
            inventory=Inventory(0.0, 2.0, 0.0, 0.0),
            max_net_volume=1.0,
            fraction=1.0,
            volume_step=0.1,
        )
        self.assertEqual(long_hedge[0].payload["side"], "sell")
        self.assertEqual(short_hedge[0].payload["side"], "buy")


if __name__ == "__main__":
    unittest.main()
