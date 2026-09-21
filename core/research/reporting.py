"""可复现的回测报告、敏感性矩阵与逐笔数据准入状态。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import json
from math import isfinite
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from typing import Iterable, Mapping, Sequence

from .history import DataQualityReport, HistoricalTick
from .models import BacktestResult, Bar
from .profiles import BacktestProfile, CostScenario
from .validation import pareto_front


UTC = timezone.utc


def _time(value: datetime | float) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromtimestamp(float(value), tz=UTC)


@dataclass(frozen=True, slots=True)
class ReviewWindow:
    """用于逐笔复核的极端连续窗口。"""

    reason: str
    start: datetime
    end: datetime
    score: float


@dataclass(frozen=True, slots=True)
class TickReviewStatus:
    """严格区分逐笔数据可用与已完成逐笔成交重放。"""

    requested_windows: int
    covered_windows: int
    status: str
    notes: tuple[str, ...] = ()

    @property
    def tick_validated(self) -> bool:
        return self.status == "逐笔成交复核完成"


@dataclass(frozen=True, slots=True)
class ReportCase:
    name: str
    instrument: str
    strategy: str
    scenario: CostScenario
    result: BacktestResult
    cost_source: str
    quality: DataQualityReport
    tick_review: TickReviewStatus


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """不持有原始行情；仅保存结果、数据质量与可复现实验参数。"""

    profile: BacktestProfile
    generated_at: datetime
    cases: tuple[ReportCase, ...]
    pareto_cases: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @classmethod
    def create(cls, profile: BacktestProfile, cases: Sequence[ReportCase], notes: Iterable[str] = ()) -> "ComparisonReport":
        baseline = {
            case.name: case.result.metrics
            for case in cases
            if case.scenario is CostScenario.BASELINE and "敏感性" not in case.strategy
        }
        return cls(
            profile=profile,
            generated_at=datetime.now(UTC),
            cases=tuple(cases),
            pareto_cases=pareto_front(baseline) if baseline else (),
            notes=tuple(notes),
        )

    def to_dict(self) -> dict[str, object]:
        def convert(value: object) -> object:
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, float) and not isfinite(value):
                return None
            if is_dataclass(value):
                return {key: convert(item) for key, item in asdict(value).items()}
            if isinstance(value, dict):
                return {str(key): convert(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [convert(item) for item in value]
            return value

        return convert(self)  # type: ignore[return-value]

    def to_markdown(self) -> str:
        lines = [
            "# XAUUSD 与 BTCUSD 标准化回测报告",
            "",
            f"区间：{self.profile.start.isoformat()} 至 {self.profile.end.isoformat()}",
            f"初始权益：{self.profile.initial_equity:,.0f} USD；年化波动目标：{self.profile.target_annual_volatility:.0%}",
            "",
            "| 案例 | 净收益/年化 | 波动率 | 最大回撤 | Sharpe | Sortino | 胜率 | 盈亏比 | PF | 风险收益比 | CVaR | 交易数 | 换手率 | 成本占比 | 佣金/滑点/swap | 逐笔状态 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
        for case in self.cases:
            item = case.result.metrics
            lines.append(
                "| {name} | {profit:,.2f} / {annual:.2%} | {vol:.2%} | {dd:.2%} | {sharpe:.2f} | {sortino:.2f} | {win:.2%} | {payoff:.2f} | {pf:.2f} | {rr:.2f} | {cvar:.2%} | {trades} | {turnover:.2f} | {cost:.2%} | {cost_mix} | {tick} |".format(
                    name=case.name,
                    profit=item.net_profit,
                    annual=item.annualized_return,
                    vol=item.volatility,
                    dd=item.max_drawdown,
                    sharpe=item.sharpe_ratio,
                    sortino=item.sortino_ratio,
                    win=item.win_rate,
                    payoff=item.payoff_ratio,
                    pf=item.profit_factor,
                    rr=item.risk_reward_ratio,
                    cvar=item.cvar,
                    trades=item.trade_count,
                    turnover=item.turnover,
                    cost=item.cost_ratio,
                    cost_mix=self._cost_mix(case),
                    tick=case.tick_review.status,
                )
            )
        matrix = self._stress_matrix()
        if matrix:
            lines.extend(
                [
                    "",
                    "## 基准与压力成本敏感性",
                    "",
                    "| 品种/策略 | 基准净收益 | 压力净收益 | 压力变化 | 基准最大回撤 | 压力最大回撤 |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for label, baseline, stress in matrix:
                lines.append(
                    f"| {label} | {baseline.net_profit:,.2f} | {stress.net_profit:,.2f} | "
                    f"{stress.net_profit - baseline.net_profit:,.2f} | {baseline.max_drawdown:.2%} | {stress.max_drawdown:.2%} |"
                )
        lines.extend(["", "## 数据与成本说明", ""])
        for case in self.cases:
            quality = case.quality
            lines.append(
                f"- {case.name}：M1 覆盖率 {quality.coverage_ratio:.2%}，缺口 {quality.gap_count}，"
                f"成本来源：{case.cost_source}；{case.tick_review.status}。"
            )
        if self.pareto_cases:
            lines.extend(["", f"Pareto 前沿（基准成本）：{', '.join(self.pareto_cases)}。"]) 
        if self.notes:
            lines.extend(["", "## 限制与说明", ""])
            lines.extend(f"- {item}" for item in self.notes)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _cost_mix(case: ReportCase) -> str:
        raw = case.result.diagnostics.get("costs", {})
        if not isinstance(raw, Mapping):
            return "—"
        commission = max(0.0, float(raw.get("commission", 0.0)))
        slippage = max(0.0, float(raw.get("slippage", 0.0)))
        swap = max(0.0, float(raw.get("swap", 0.0)))
        total = commission + slippage + swap
        if total <= 1e-12:
            return "0% / 0% / 0%"
        return f"{commission / total:.0%} / {slippage / total:.0%} / {swap / total:.0%}"

    def _stress_matrix(self):
        grouped: dict[tuple[str, str], dict[CostScenario, ReportCase]] = {}
        for case in self.cases:
            grouped.setdefault((case.instrument, case.strategy), {})[case.scenario] = case
        result = []
        for (instrument, strategy), values in sorted(grouped.items()):
            baseline, stress = values.get(CostScenario.BASELINE), values.get(CostScenario.STRESS)
            if baseline is not None and stress is not None:
                result.append((f"{instrument} / {strategy}", baseline.result.metrics, stress.result.metrics))
        return tuple(result)

    def write(self, output_directory: Path) -> tuple[Path, Path]:
        """显式写入 JSON/Markdown；调用方决定目录，不隐式落盘。"""

        output_directory.mkdir(parents=True, exist_ok=True)
        json_path = output_directory / "xau_btc_backtest_report.json"
        markdown_path = output_directory / "xau_btc_backtest_report.md"
        json_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, markdown_path


def select_extreme_windows(
    bars: Sequence[Bar],
    *,
    days: int = 7,
    count: int = 3,
    trade_times: Iterable[datetime | float] = (),
) -> tuple[ReviewWindow, ...]:
    """选择最大回撤、最高波动、最高交易密度的连续窗口。"""

    ordered = sorted(bars, key=lambda item: _time(item.timestamp))
    if not ordered:
        return ()
    bar_stamps = tuple(_time(item.timestamp) for item in ordered)
    closes = tuple(item.close for item in ordered)
    trade_stamps = sorted(_time(item) for item in trade_times)
    span = timedelta(days=days)
    candidates: list[tuple[ReviewWindow, ReviewWindow, ReviewWindow]] = []
    left = 0
    for right in range(len(ordered)):
        end = bar_stamps[right]
        while left < right and bar_stamps[left] < end - span:
            left += 1
        if end - bar_stamps[left] < span - timedelta(minutes=5):
            continue
        peak = closes[left]
        drawdown = 0.0
        for close in closes[left : right + 1]:
            peak = max(peak, close)
            drawdown = max(drawdown, (peak - close) / max(peak, 1e-12))
        returns = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(left + 1, right + 1)
        ]
        volatility = pstdev(returns) if len(returns) > 1 else 0.0
        start = bar_stamps[left]
        density = bisect_right(trade_stamps, end) - bisect_left(trade_stamps, start)
        candidates.append(
            (
                ReviewWindow("最大回撤", start, end, drawdown),
                ReviewWindow("最高波动", start, end, volatility),
                ReviewWindow("最高交易密度", start, end, float(density)),
            )
        )
    selected: list[ReviewWindow] = []
    for index in range(3):
        for candidate in sorted((item[index] for item in candidates), key=lambda item: item.score, reverse=True)[:count]:
            if not any(item.reason == candidate.reason and item.start == candidate.start for item in selected):
                selected.append(candidate)
    return tuple(selected)


def assess_tick_coverage(
    windows: Sequence[ReviewWindow],
    ticks: Sequence[HistoricalTick],
    *,
    execution_replayed: bool = False,
) -> TickReviewStatus:
    """校验逐笔历史是否足以重放；未重放时绝不标记为“已验证”。"""

    if not windows:
        return TickReviewStatus(0, 0, "未请求逐笔复核", ("没有可选择的极端窗口",))
    ordered = sorted(ticks, key=lambda item: item.timestamp)
    covered = 0
    for window in windows:
        in_window = [item for item in ordered if window.start <= item.timestamp <= window.end]
        if in_window and in_window[0].timestamp <= window.start + timedelta(minutes=5) and in_window[-1].timestamp >= window.end - timedelta(minutes=5):
            covered += 1
    if covered != len(windows):
        return TickReviewStatus(
            len(windows),
            covered,
            "M1 结论（逐笔历史缺失）",
            ("缺少完整逐笔窗口，报告不会标记为逐笔已验证",),
        )
    if execution_replayed:
        return TickReviewStatus(
            len(windows),
            covered,
            "逐笔成交复核完成",
            ("极端窗口以时间排序的 bid/ask tick 路径替换 M1 盘中路径后已重放",),
        )
    return TickReviewStatus(
        len(windows),
        covered,
        "M1 结论（逐笔数据可供复核）",
        ("逐笔数据覆盖校验已通过；需使用逐笔成交重放器后才能标记为逐笔成交复核完成",),
    )
