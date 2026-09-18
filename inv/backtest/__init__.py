"""回测引擎与优化器。

核心原则：
1. 严格模拟时间推进，禁止事后决策
2. 每个时间窗口独立运行，逐步向前推进
3. 手续费、滑点、仓位约束完整模拟
"""

from .engine import BacktestEngine
from .optimizer import (
    ParameterOptimizer, WalkForwardOptimizer,
    ParameterGrid, OptimizationResult,
    find_best_params, params_summary,
)
from .metrics import calculate_metrics
from .reporter import BacktestReporter

__all__ = [
    "BacktestEngine",
    "ParameterOptimizer",
    "WalkForwardOptimizer",
    "ParameterGrid",
    "OptimizationResult",
    "find_best_params",
    "params_summary",
    "calculate_metrics",
    "BacktestReporter",
]