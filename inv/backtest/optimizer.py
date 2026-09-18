"""参数优化器与 Walk-Forward 交叉验证框架。

支持：
1. 网格搜索：遍历参数组合进行回测
2. 随机搜索：在参数空间中随机采样
3. Walk-Forward：滚动时间窗口验证
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from math import isnan
from random import Random
from typing import Any, Callable, Sequence

from inv.domain.models import BacktestResult, Bar, SymbolSpec
from inv.domain.settings import StrategySettings
from inv.strategy.base import BaseStrategy
from inv.backtest.engine import BacktestEngine


@dataclass
class OptimizationResult:
    """单次优化的完整结果。"""

    params: dict[str, Any]
    result: BacktestResult
    score: float

    def __lt__(self, other: OptimizationResult) -> bool:
        return self.score < other.score


class ParameterGrid:
    """参数网格生成器。"""

    def __init__(
        self,
        param_ranges: dict[str, list[Any] | tuple[float, float, int]],
    ) -> None:
        """
        参数:
            param_ranges: 参数范围字典
                - 值类型为 list → 直接枚举
                - 值类型为 (start, stop, count) → 线性插值生成 count 个点
        """
        self.param_ranges = param_ranges
        self._expanded: dict[str, list[Any]] = {}
        for key, value in param_ranges.items():
            if isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], (int, float)):
                # 范围描述: (start, stop, count)
                start, stop, count_ = value
                count_int = max(1, int(count_))
                if count_int <= 1:
                    self._expanded[key] = [start]
                else:
                    step = (stop - start) / (count_int - 1)
                    self._expanded[key] = [start + step * i for i in range(count_int)]
            else:
                self._expanded[key] = list(value)

    @property
    def total_combinations(self) -> int:
        count = 1
        for values in self._expanded.values():
            count *= len(values)
        return count

    def all_params(self) -> list[dict[str, Any]]:
        """生成所有参数组合。"""
        keys = list(self._expanded.keys())
        value_lists = [self._expanded[k] for k in keys]
        results = []
        for combo in product(*value_lists):
            results.append(dict(zip(keys, combo)))
        return results

    def sample(self, n: int, seed: int = 42) -> list[dict[str, Any]]:
        """随机采样 n 组参数。"""
        all_combos = self.all_params()
        if n >= len(all_combos):
            return all_combos
        rng = Random(seed)
        return rng.sample(all_combos, n)


class ParameterOptimizer:
    """参数优化器，支持网格搜索和随机搜索。"""

    def __init__(
        self,
        engine: BacktestEngine,
        strategy_factory: Callable[[dict[str, Any]], BaseStrategy],
        param_grid: ParameterGrid,
        settings_factory: Callable[[dict[str, Any]], StrategySettings] | None = None,
        scoring_fn: Callable[[BacktestResult], float] | None = None,
    ) -> None:
        """
        参数:
            engine: 回测引擎
            strategy_factory: 参数 → 策略实例
            param_grid: 参数网格
            settings_factory: 参数 → StrategySettings（可选，默认使用基础配置）
            scoring_fn: 评分函数，输入 BacktestResult，输出得分（越高越好）
        """
        self.engine = engine
        self.strategy_factory = strategy_factory
        self.param_grid = param_grid
        self.settings_factory = settings_factory
        self.scoring_fn = scoring_fn or self._default_scoring

    @staticmethod
    def _default_scoring(result: BacktestResult) -> float:
        """默认评分：夏普比率 + 收益/回撤 综合。"""
        m = result.metrics
        score = 0.0

        # 夏普比率（权重 0.3）
        if m.sharpe_ratio > 0:
            score += 0.3 * min(m.sharpe_ratio / 3.0, 1.0)

        # 收益/回撤比（权重 0.3）
        if m.risk_reward_ratio > 0:
            score += 0.3 * min(m.risk_reward_ratio / 5.0, 1.0)

        # 胜率（权重 0.2）
        score += 0.2 * m.win_rate

        # 利润因子（权重 0.2）
        if m.profit_factor > 1:
            score += 0.2 * min((m.profit_factor - 1) / 2.0, 1.0)
        elif m.profit_factor < 1 and m.trade_count > 0:
            score -= 0.1

        # 交易次数惩罚（少于 10 次视为样本不足）
        if m.trade_count < 10:
            score *= max(0.0, m.trade_count / 10.0)

        return score

    def run(
        self,
        bars: Sequence[Bar],
        symbol_spec: SymbolSpec | None = None,
        method: str = "grid",
        n_samples: int = 50,
        seed: int = 42,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[OptimizationResult]:
        """运行参数优化。

        参数:
            bars: K 线数据
            symbol_spec: 合约规格
            method: "grid"（全网格）或 "random"（随机采样）
            n_samples: 随机采样数量
            seed: 随机种子
            progress_callback: 进度回调

        返回:
            按评分从高到低排序的结果列表
        """
        if method == "random":
            param_list = self.param_grid.sample(n_samples, seed)
        elif method == "grid":
            param_list = self.param_grid.all_params()
        else:
            raise ValueError(f"不支持的搜索方法: {method}")

        results: list[OptimizationResult] = []
        total = len(param_list)

        for idx, params in enumerate(param_list):
            strategy = self.strategy_factory(params)
            settings = (
                self.settings_factory(params)
                if self.settings_factory
                else StrategySettings()
            )
            result = self.engine.run(strategy, bars, settings, symbol_spec)
            score = self.scoring_fn(result)

            results.append(OptimizationResult(
                params=params, result=result, score=score,
            ))

            if progress_callback:
                progress_callback(idx + 1, total)

        results.sort(reverse=True)
        return results


@dataclass
class WalkForwardWindow:
    """Walk-Forward 的单个训练/测试窗口。"""

    train_start: int
    train_end: int
    test_start: int
    test_end: int


class WalkForwardOptimizer:
    """Walk-Forward 滚动验证优化器。

    将数据划分为多个滚动窗口，每个窗口包含训练集和测试集：
    - 训练集：用于寻找最优参数
    - 测试集：用于验证最优参数的样本外表现
    """

    def __init__(
        self,
        optimizer: ParameterOptimizer,
        window_size: int = 252,  # 约 1 年日线数据
        test_size: int = 63,  # 约 3 个月
        step_size: int = 63,  # 步长 3 个月
    ) -> None:
        self.optimizer = optimizer
        self.window_size = int(window_size)
        self.test_size = int(test_size)
        self.step_size = int(step_size)

    def windows(self, total_bars: int) -> list[WalkForwardWindow]:
        """生成滚动窗口索引列表。"""
        windows_list = []
        test_start = self.window_size
        while test_start + self.test_size <= total_bars:
            windows_list.append(WalkForwardWindow(
                train_start=0,
                train_end=test_start,  # 训练用所有历史数据
                test_start=test_start,
                test_end=test_start + self.test_size,
            ))
            test_start += self.step_size
        return windows_list

    def run(
        self,
        bars: Sequence[Bar],
        symbol_spec: SymbolSpec | None = None,
        method: str = "random",
        n_samples: int = 50,
        in_sample_best_n: int = 5,
        seed: int = 42,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[OptimizationResult]:
        """运行 Walk-Forward 验证。

        流程:
            1. 在每个训练窗口上执行参数优化
            2. 取训练集上评分最高的 in_sample_best_n 组参数
            3. 在对应的测试集上验证
            4. 汇总所有测试集的验证结果
        """
        bars_list = list(bars)
        total = len(bars_list)
        wfs = self.windows(total)

        all_results: list[OptimizationResult] = []

        for w_idx, window in enumerate(wfs):
            train_bars = bars_list[window.train_start:window.train_end]
            test_bars = bars_list[window.test_start:window.test_end]

            if len(train_bars) < 30 or len(test_bars) < 5:
                continue

            # 在训练集上优化
            train_results = self.optimizer.run(
                bars=train_bars,
                symbol_spec=symbol_spec,
                method=method,
                n_samples=n_samples,
                seed=seed + w_idx,
            )

            # 取最优的几组参数在测试集上验证
            for best in train_results[:in_sample_best_n]:
                strategy = self.optimizer.strategy_factory(best.params)
                settings = (
                    self.optimizer.settings_factory(best.params)
                    if self.optimizer.settings_factory
                    else StrategySettings()
                )
                test_result = self.optimizer.engine.run(
                    strategy, test_bars, settings, symbol_spec
                )
                test_score = self.optimizer.scoring_fn(test_result)

                all_results.append(OptimizationResult(
                    params=best.params,
                    result=test_result,
                    score=test_score,
                ))

            if progress_callback:
                progress_callback(w_idx + 1, len(wfs))

        all_results.sort(reverse=True)
        return all_results


def find_best_params(
    results: list[OptimizationResult], top_n: int = 5
) -> list[OptimizationResult]:
    """返回评分最高的 N 组参数。"""
    return sorted(results, reverse=True)[:top_n]


def params_summary(results: list[OptimizationResult], top_n: int = 10) -> str:
    """生成参数优化摘要。"""
    top = find_best_params(results, top_n)
    lines = ["参数优化结果（Top {}）：".format(len(top)), "-" * 80]
    for idx, opt in enumerate(top, start=1):
        m = opt.result.metrics
        lines.append(
            f"{idx:>3}. 评分={opt.score:>6.3f}  |  "
            f"收益={m.net_profit:>8.2f}  |  夏普={m.sharpe_ratio:>5.2f}  |  "
            f"回撤={m.max_drawdown:>5.2%}  |  交易={m.trade_count:>4d}  |  "
            f"参数={opt.params}"
        )
    return "\n".join(lines)


__all__ = [
    "ParameterGrid", "ParameterOptimizer",
    "WalkForwardOptimizer", "OptimizationResult",
    "find_best_params", "params_summary",
]