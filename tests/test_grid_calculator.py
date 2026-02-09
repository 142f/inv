import unittest

from core.strategy.components.grid_calculator import GridCalculator


class GridCalculatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calc = GridCalculator(lambda price: round(float(price), 2))

    def test_build_targets_neutral(self):
        buys, sells = self.calc.build_targets(
            anchor=100.0,
            step=1.0,
            min_price=90.0,
            max_price=110.0,
            bid=99.0,
            ask=101.0,
            buy_window=2,
            sell_window=2,
            mode="neutral",
            recenter_steps=3,
        )
        self.assertEqual(buys, [100.0, 99.0])
        self.assertEqual(sells, [101.0, 100.0])

    def test_blocked_levels_are_skipped(self):
        buys, sells = self.calc.build_targets(
            anchor=100.0,
            step=1.0,
            min_price=90.0,
            max_price=110.0,
            bid=99.0,
            ask=101.0,
            buy_window=2,
            sell_window=2,
            mode="neutral",
            recenter_steps=3,
            blocked_k={0},
        )
        self.assertEqual(buys, [99.0, 98.0])
        self.assertEqual(sells, [102.0, 101.0])

    def test_min_distance_filters_nearby_levels(self):
        buys, sells = self.calc.build_targets(
            anchor=100.0,
            step=1.0,
            min_price=90.0,
            max_price=110.0,
            bid=99.0,
            ask=101.0,
            buy_window=2,
            sell_window=2,
            mode="neutral",
            recenter_steps=3,
            min_dist=2.0,
        )
        self.assertNotIn(100.0, buys)
        self.assertNotIn(100.0, sells)


if __name__ == "__main__":
    unittest.main()
