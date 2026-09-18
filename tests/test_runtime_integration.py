from __future__ import annotations

from types import SimpleNamespace
import unittest

import numpy as np

from core.runtime import DataFeed
from core.strategy.manager import build_strategy


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _DataBroker:
    def __init__(self) -> None:
        self.lock = _Lock()
        self.rate_calls = 0

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.rate_calls += 1
        rates = np.zeros(count, dtype=[("time", "i8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
        rates["high"] = 2.0
        rates["low"] = 1.0
        rates["close"] = 1.5
        return rates


class _Gateway:
    def __init__(self) -> None:
        self.calls = []

    def call(self, name, *args, **kwargs):
        self.calls.append(name)
        if name == "symbol_info":
            return SimpleNamespace(
                digits=2,
                point=0.01,
                trade_stops_level=0,
                volume_min=0.1,
                volume_max=10.0,
                volume_step=0.1,
                filling_mode=None,
            )
        if name == "symbol_info_tick":
            return SimpleNamespace(bid=10.0, ask=10.01, time=1)
        raise AssertionError(f"意外的网关调用: {name}")


class RuntimeIntegrationTests(unittest.TestCase):
    def test_datafeed_reuses_rates_within_cache_window(self) -> None:
        broker = _DataBroker()
        datafeed = DataFeed(broker)
        self.assertEqual(len(datafeed.get_rates("TEST", 1, 10, cache_seconds=30.0)), 10)
        self.assertEqual(len(datafeed.get_rates("TEST", 1, 10, cache_seconds=30.0)), 10)
        self.assertEqual(broker.rate_calls, 1)

    def test_legacy_strategy_receives_type_snapshot_and_gateway(self) -> None:
        gateway = _Gateway()
        strategy = build_strategy(
            {"symbol": "TEST", "magic": 1, "step": 0.1, "tp_dist": 0.1, "lot": 0.1},
            gateway=gateway,
        )
        self.assertEqual(strategy.settings.strategy_id, "1:TEST")
        self.assertIs(strategy.gateway, gateway)
        self.assertEqual(strategy._get_tick().bid, 10.0)
        self.assertIn("symbol_info", gateway.calls)
        self.assertIn("symbol_info_tick", gateway.calls)


if __name__ == "__main__":
    unittest.main()
