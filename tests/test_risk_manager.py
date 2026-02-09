import unittest

from core.strategy.components.risk_manager import RiskManager


class RiskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rm = RiskManager()

    def test_neutral_mode_over_cap_allows_only_reducing_abs_net(self):
        common = {
            "long_vol": 2.0,
            "short_vol": 0.0,
            "pending_buy_vol": 0.0,
            "pending_sell_vol": 0.0,
            "lot": 0.1,
            "mode": "neutral",
            "max_net_vol": 1.5,
        }

        self.assertFalse(self.rm.check_inventory_limits(side="buy", **common))
        self.assertTrue(self.rm.check_inventory_limits(side="sell", **common))

    def test_long_mode_blocks_reverse_side_without_hedge(self):
        allowed = self.rm.check_inventory_limits(
            long_vol=2.0,
            short_vol=0.0,
            pending_buy_vol=0.0,
            pending_sell_vol=0.0,
            lot=0.5,
            side="sell",
            mode="long",
            max_net_vol=2.0,
            hedge_enabled=False,
        )
        self.assertFalse(allowed)

    def test_long_mode_hedge_allows_reverse_if_net_not_negative(self):
        allowed = self.rm.check_inventory_limits(
            long_vol=2.0,
            short_vol=0.0,
            pending_buy_vol=0.0,
            pending_sell_vol=0.0,
            lot=0.5,
            side="sell",
            mode="long",
            max_net_vol=2.0,
            hedge_enabled=True,
        )
        blocked = self.rm.check_inventory_limits(
            long_vol=2.0,
            short_vol=0.0,
            pending_buy_vol=0.0,
            pending_sell_vol=0.0,
            lot=3.0,
            side="sell",
            mode="long",
            max_net_vol=2.0,
            hedge_enabled=True,
        )
        self.assertTrue(allowed)
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
