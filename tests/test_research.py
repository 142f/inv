from __future__ import annotations

import unittest

from core.research import Bar, BarBacktester, CostModel, pareto_front, walk_forward_splits
from core.research.simulator import SimOrder


class ResearchTests(unittest.TestCase):
    def test_backtest_includes_cost_and_uses_conservative_path(self) -> None:
        bars = [
            Bar(1.0, 10.0, 10.1, 9.9, 10.0, spread=0.01, volume=100.0),
            Bar(2.0, 10.0, 10.3, 9.8, 10.2, spread=0.01, volume=100.0),
        ]
        issued = False

        def decide(bar):
            nonlocal issued
            if issued:
                return ()
            issued = True
            return (SimOrder("buy", 9.9, 1.0, take_profit=10.2, stop_loss=9.8),)

        result = BarBacktester(CostModel(commission_per_volume=0.01, point=0.01)).run(bars, decide)
        self.assertEqual(result.metrics.trade_count, 1)
        self.assertGreater(result.metrics.cost_ratio, 0.0)
        self.assertIn(result.diagnostics["path"], {"OHLC", "OLHC"})

    def test_walk_forward_has_no_future_overlap(self) -> None:
        splits = walk_forward_splits(100, train_size=40, validate_size=20, test_size=10)
        self.assertEqual(splits[0].train.stop, splits[0].validate.start)
        self.assertEqual(splits[0].validate.stop, splits[0].test.start)
        self.assertGreater(len(splits), 1)


if __name__ == "__main__":
    unittest.main()
