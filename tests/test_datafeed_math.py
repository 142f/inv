import unittest
import sys
import types

import numpy as np

# `core.runtime` imports Runner, which imports MetaTrader5.
# Provide a lightweight stub so pure math tests can run without MT5 installed.
if "MetaTrader5" not in sys.modules:
    sys.modules["MetaTrader5"] = types.ModuleType("MetaTrader5")

from core.runtime.datafeed import DataFeed


class DataFeedMathTests(unittest.TestCase):
    def test_calculate_raw_atr_sma(self):
        tr = np.array([1.0, 2.0, 3.0, 4.0], dtype=float)
        self.assertAlmostEqual(DataFeed._calculate_raw_atr(tr, 2, "sma"), 3.5)

    def test_calculate_raw_atr_ema(self):
        tr = np.array([1.0, 2.0, 3.0, 4.0], dtype=float)
        self.assertAlmostEqual(DataFeed._calculate_raw_atr(tr, 2, "ema"), 3.5)

    def test_calculate_raw_atr_wilder(self):
        tr = np.array([1.0, 2.0, 3.0, 4.0], dtype=float)
        self.assertAlmostEqual(DataFeed._calculate_raw_atr(tr, 2, "wilder"), 3.125)

    def test_smooth_atr(self):
        self.assertEqual(DataFeed._smooth_atr(2.0, None, 0.5), 2.0)
        self.assertEqual(DataFeed._smooth_atr(2.0, 1.0, 0.5), 1.5)
        self.assertEqual(DataFeed._smooth_atr(2.0, 1.0, 0.0), 2.0)


if __name__ == "__main__":
    unittest.main()
