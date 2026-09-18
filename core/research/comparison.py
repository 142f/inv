"""XAUUSD 与 BTCUSD 的标准化实验编排器。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Sequence

from .benchmark import EqualRiskBuyHoldBacktester
from .grid_backtest import GridBacktestAdapter
from .history import HistoricalTick, HistoryProvider, validate_m1_bars
from .profiles import (
    BTCUSD_STANDARD_COST,
    XAUUSD_STANDARD_COST,
    BacktestProfile,
    CostScenario,
    xau_swap_sensitivity,
)
from .reporting import (
    ComparisonReport,
    ReportCase,
    assess_tick_coverage,
    select_extreme_windows,
)


class InsufficientHistoryError(ValueError):
    """关键 M1 覆盖不达标时拒绝输出横向策略结论。"""


class StandardComparisonRunner:
    """只通过 ``HistoryProvider`` 取得行情，负责统一资金、风险与报告口径。"""

    def __init__(self, provider: HistoryProvider, profile: BacktestProfile | None = None) -> None:
        self.provider = provider
        self.profile = profile or BacktestProfile()

    def run(self, *, xau_suffix: str = "", btc_suffix: str = "", request_tick_review: bool = True) -> ComparisonReport:
        pairs = (
            ("XAU", self.profile.resolve_symbol(self.profile.xau_symbol, xau_suffix), XAUUSD_STANDARD_COST, False),
            ("BTC", self.profile.resolve_symbol(self.profile.btc_symbol, btc_suffix), BTCUSD_STANDARD_COST, True),
        )
        cases: list[ReportCase] = []
        notes = [
            "结果为同等初始权益和波动预算下的模拟，不构成收益承诺。",
            "OHLC 与 OLHC 两条盘中路径均已运行，网格结果采用最终权益较低的一条。",
        ]
        for short_name, symbol, cost, trades_24_7 in pairs:
            cases.extend(
                self._run_instrument(
                    short_name,
                    symbol,
                    cost,
                    trades_24_7=trades_24_7,
                    request_tick_review=request_tick_review,
                )
            )
        return ComparisonReport.create(self.profile, cases, notes)

    def _run_instrument(self, short_name: str, symbol: str, cost, *, trades_24_7: bool, request_tick_review: bool) -> Sequence[ReportCase]:
        spec = self.provider.specification(symbol)
        bars = tuple(self.provider.bars(symbol, self.profile.start, self.profile.end, "M1"))
        quality = validate_m1_bars(symbol, bars, self.profile.start, self.profile.end, trades_24_7=trades_24_7)
        if not quality.is_usable or quality.coverage_ratio < self.profile.required_m1_coverage:
            raise InsufficientHistoryError(
                f"{symbol} 的 M1 数据不满足正式横向结论要求：覆盖率 {quality.coverage_ratio:.2%}，"
                f"异常 {quality.invalid_bars}，缺口 {quality.gap_count}"
            )
        cleaned = self._clean_bars(bars)
        adapter = GridBacktestAdapter(self.profile, spec, cost)
        base_grid = adapter.run(cleaned, scenario=CostScenario.BASELINE)
        tick_status, tick_overrides, review_windows = self._tick_overrides(
            cleaned, base_grid.conservative.result, symbol, request_tick_review
        )
        replay_overrides = {}
        if tick_status.covered_windows == tick_status.requested_windows and tick_overrides:
            replay_overrides = tick_overrides
            base_grid = adapter.run(cleaned, scenario=CostScenario.BASELINE, tick_overrides=replay_overrides)
            stress_grid = adapter.run(cleaned, scenario=CostScenario.STRESS, tick_overrides=replay_overrides)
            tick_status = assess_tick_coverage(
                review_windows,
                tuple(tick for group in tick_overrides.values() for tick in group),
                execution_replayed=True,
            )
        else:
            stress_grid = adapter.run(cleaned, scenario=CostScenario.STRESS)
        baseline_benchmark = EqualRiskBuyHoldBacktester(self.profile, spec, cost).run(cleaned, scenario=CostScenario.BASELINE)
        stress_benchmark = EqualRiskBuyHoldBacktester(self.profile, spec, cost).run(cleaned, scenario=CostScenario.STRESS)
        actual_swap = (
            spec.swap_long is not None
            and spec.swap_short is not None
            and spec.swap_mode in {"points", "currency_deposit"}
        )
        describe_source = getattr(self.provider, "describe_source", lambda _: "MT5 历史行情")
        source = (
            f"{describe_source(symbol)}；{cost.source}；"
            f"{'隔夜费采用 MT5 品种规格' if actual_swap else '隔夜费采用公开回退/敏感性假设'}"
        )
        result: list[ReportCase] = [
            ReportCase(
                f"{short_name} 网格（基准成本）",
                symbol,
                "中性双向网格",
                CostScenario.BASELINE,
                base_grid.conservative.result,
                source,
                quality,
                tick_status,
            ),
            ReportCase(
                f"{short_name} 网格（压力成本）",
                symbol,
                "中性双向网格",
                CostScenario.STRESS,
                stress_grid.conservative.result,
                source,
                quality,
                tick_status,
            ),
            ReportCase(
                f"{short_name} 等风险买入持有（基准成本）",
                symbol,
                "等风险买入持有",
                CostScenario.BASELINE,
                baseline_benchmark,
                source,
                quality,
                tick_status,
            ),
            ReportCase(
                f"{short_name} 等风险买入持有（压力成本）",
                symbol,
                "等风险买入持有",
                CostScenario.STRESS,
                stress_benchmark,
                source,
                quality,
                tick_status,
            ),
        ]
        # XAU 未返回经纪商隔夜规则时，明确展示三个替代情景，而不把它们伪装成真实经纪商费用。
        if short_name == "XAU" and not actual_swap:
            for daily_swap in xau_swap_sensitivity():
                sensitivity_spec = replace(
                    spec,
                    swap_long=daily_swap,
                    swap_short=daily_swap,
                    swap_mode="currency_deposit",
                )
                sensitivity = GridBacktestAdapter(self.profile, sensitivity_spec, cost).run(
                    cleaned, scenario=CostScenario.BASELINE, tick_overrides=replay_overrides
                )
                result.append(
                    ReportCase(
                        f"XAU 网格（swap {daily_swap:.0f} USD/lot/日）",
                        symbol,
                        "中性双向网格：隔夜费敏感性",
                        CostScenario.BASELINE,
                        sensitivity.conservative.result,
                        f"{cost.source}；XAU swap 人工敏感性，非经纪商实际费用",
                        quality,
                        tick_status,
                    )
                )
        return result

    def _review_windows(self, bars, result):
        return select_extreme_windows(
            bars,
            days=self.profile.tick_review_days,
            count=self.profile.tick_review_windows,
            trade_times=(trade.exit_time for trade in result.trades),
        )

    def _tick_overrides(self, bars, result, symbol: str, requested: bool):
        if not requested:
            return assess_tick_coverage((), ()), {}, ()
        windows = self._review_windows(bars, result)
        ticks: list[HistoricalTick] = []
        for window in windows:
            ticks.extend(self.provider.ticks(symbol, window.start, window.end))
        overrides: dict[datetime, list[HistoricalTick]] = {}
        seen: set[tuple[datetime, float, float, float, float]] = set()
        for tick in ticks:
            identity = (tick.timestamp, tick.bid, tick.ask, tick.last, tick.volume)
            if identity in seen:
                continue
            seen.add(identity)
            overrides.setdefault(tick.timestamp.replace(second=0, microsecond=0), []).append(tick)
        return assess_tick_coverage(windows, ticks), overrides, windows

    @staticmethod
    def _clean_bars(bars):
        # 校验已报告重复项；回测只使用排序后的第一根，避免重复报价改变结果。
        ordered = sorted(bars, key=lambda item: item.timestamp)
        result = []
        seen = set()
        for item in ordered:
            if item.timestamp in seen:
                continue
            seen.add(item.timestamp)
            result.append(item)
        return tuple(result)
