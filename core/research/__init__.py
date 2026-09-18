"""回测、成本建模与多目标策略评估。"""

from .metrics import calculate_metrics
from .models import BacktestResult, Bar, CostModel, Trade
from .simulator import BarBacktester
from .validation import pareto_front, select_robust_candidate, walk_forward_splits
from .benchmark import EqualRiskBuyHoldBacktester
from .comparison import InsufficientHistoryError, StandardComparisonRunner
from .dukascopy import DukascopyBi5HistoryProvider, DukascopyDataError
from .grid_backtest import ConservativeGridResult, GridBacktestAdapter, GridBacktestRun
from .history import (
    DataQualityReport,
    HistoricalTick,
    HistoryUnavailableError,
    HistoryProvider,
    InstrumentSpec,
    Mt5HistoryProvider,
    validate_m1_bars,
)
from .profiles import (
    BTCUSD_STANDARD_COST,
    XAUUSD_STANDARD_COST,
    BacktestProfile,
    CommissionUnit,
    CostProfile,
    CostScenario,
    xau_swap_sensitivity,
)
from .reporting import ComparisonReport, ReportCase, TickReviewStatus, assess_tick_coverage, select_extreme_windows
from .symbols import resolve_broker_symbol

__all__ = [
    "BacktestResult",
    "BacktestProfile",
    "Bar",
    "BarBacktester",
    "BTCUSD_STANDARD_COST",
    "CommissionUnit",
    "ComparisonReport",
    "ConservativeGridResult",
    "CostProfile",
    "CostModel",
    "CostScenario",
    "DataQualityReport",
    "DukascopyBi5HistoryProvider",
    "DukascopyDataError",
    "EqualRiskBuyHoldBacktester",
    "GridBacktestAdapter",
    "GridBacktestRun",
    "HistoricalTick",
    "HistoryUnavailableError",
    "HistoryProvider",
    "InsufficientHistoryError",
    "InstrumentSpec",
    "Mt5HistoryProvider",
    "ReportCase",
    "StandardComparisonRunner",
    "TickReviewStatus",
    "Trade",
    "XAUUSD_STANDARD_COST",
    "calculate_metrics",
    "assess_tick_coverage",
    "pareto_front",
    "select_robust_candidate",
    "select_extreme_windows",
    "resolve_broker_symbol",
    "validate_m1_bars",
    "walk_forward_splits",
    "xau_swap_sensitivity",
]
