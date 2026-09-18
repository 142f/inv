"""预定义的 Pipeline 阶段实现。

每个阶段实现 PipelineStageProtocol 接口，职责单一、可独立运行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from inv.domain.models import Bar, SignalType
from inv.domain.settings import StrategySettings
from inv.strategy.base import BaseStrategy
from inv.backtest.engine import BacktestEngine
from inv.backtest.metrics import calculate_metrics, metrics_summary
from inv.backtest.reporter import BacktestReporter
from inv.pipeline.base import PipelineStageProtocol, StageInput, StageOutput


class DataLoadingStage(PipelineStageProtocol):
    """阶段 1：数据加载与预处理。"""

    def __init__(self, name: str = "data_loading") -> None:
        self._name = name

    def stage_name(self) -> str:
        return self._name

    def execute(self, stage_input: StageInput) -> StageOutput:
        # 模拟数据加载：实际可从文件/数据库读取
        bars = list(stage_input.bars)
        config = stage_input.config

        # 数据质量检查
        issues: list[str] = []
        if not bars:
            issues.append("无 K 线数据")
        else:
            # 检查时间顺序
            timestamps = [b.timestamp for b in bars]
            for i in range(1, len(timestamps)):
                if isinstance(timestamps[i], (int, float)) and isinstance(timestamps[i - 1], (int, float)):
                    if timestamps[i] <= timestamps[i - 1]:
                        issues.append(f"时间戳顺序异常: 索引 {i-1} -> {i}")
                        break

            # 检查异常价格
            for b in bars:
                if any(v <= 0 for v in [b.open, b.high, b.low, b.close]):
                    issues.append(f"异常价格: {b}")
                    break

        return StageOutput(
            stage_name=self._name,
            symbol=stage_input.symbol,
            timestamp=stage_input.start_time,
            data={"bars": tuple(bars), "bar_count": len(bars)},
            diagnostics={
                "data_issues": issues,
                "data_quality": "ok" if not issues else "warning",
            },
        )


class StrategySignalStage(PipelineStageProtocol):
    """阶段 2：策略信号生成。"""

    def __init__(
        self,
        strategy: BaseStrategy,
        settings: StrategySettings,
        name: str = "strategy_signals",
    ) -> None:
        self._name = name
        self.strategy = strategy
        self.settings = settings

    def stage_name(self) -> str:
        return self._name

    def execute(self, stage_input: StageInput) -> StageOutput:
        bars = list(stage_input.bars)
        config = dict(stage_input.config)

        # 如果配置中有策略参数，覆盖默认值（StrategySettings 是 frozen dataclass）
        from dataclasses import asdict
        strategy_params = {}
        for field_name in config:
            if hasattr(self.settings, field_name):
                strategy_params[field_name] = config[field_name]
        if strategy_params:
            merged = {**asdict(self.settings), **strategy_params}
            self.settings = StrategySettings(**merged)

        # 从头运行策略，生成所有信号
        self.strategy.reset_state()
        all_signals: list[SignalType] = []

        for i in range(len(bars)):
            signals = self.strategy.compute_signals(bars, i, self.settings)
            for sig in signals:
                all_signals.append(sig.signal_type)

        # 统计信号分布
        signal_counts: dict[str, int] = {}
        for st in all_signals:
            key = st.value if hasattr(st, "value") else str(st)
            signal_counts[key] = signal_counts.get(key, 0) + 1

        return StageOutput(
            stage_name=self._name,
            symbol=stage_input.symbol,
            timestamp=stage_input.end_time,
            signals=tuple(all_signals),
            metrics={"signal_count": len(all_signals)},
            diagnostics={"signal_distribution": signal_counts},
        )


class BacktestExecutionStage(PipelineStageProtocol):
    """阶段 3：回测执行。"""

    def __init__(
        self,
        strategy: BaseStrategy,
        settings: StrategySettings,
        engine: BacktestEngine | None = None,
        name: str = "backtest_execution",
    ) -> None:
        self._name = name
        self.strategy = strategy
        self.settings = settings
        self.engine = engine or BacktestEngine()

    def stage_name(self) -> str:
        return self._name

    def execute(self, stage_input: StageInput) -> StageOutput:
        from inv.domain.models import SymbolSpec

        bars = list(stage_input.bars)
        symbol_spec = SymbolSpec(
            symbol=stage_input.symbol,
            price_tick=0.00001,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )

        result = self.engine.run(
            self.strategy, bars, self.settings, symbol_spec
        )

        return StageOutput(
            stage_name=self._name,
            symbol=stage_input.symbol,
            timestamp=stage_input.end_time,
            data={
                "backtest_result": result,
                "equity_curve": result.equity_curve,
                "trades": result.trades,
            },
            metrics={
                "net_profit": result.metrics.net_profit,
                "sharpe_ratio": result.metrics.sharpe_ratio,
                "max_drawdown": result.metrics.max_drawdown,
                "win_rate": result.metrics.win_rate,
                "trade_count": result.metrics.trade_count,
            },
            diagnostics=dict(result.diagnostics),
        )


class PerformanceEvaluationStage(PipelineStageProtocol):
    """阶段 4：绩效评估。"""

    def __init__(self, name: str = "performance_evaluation") -> None:
        self._name = name

    def stage_name(self) -> str:
        return self._name

    def execute(self, stage_input: StageInput) -> StageOutput:
        # 前一个阶段（回测执行）的输出放在 previous_output 中
        prev_data = stage_input.previous_output.get("data", {})
        result = prev_data.get("backtest_result")

        if result is None:
            return StageOutput(
                stage_name=self._name,
                symbol=stage_input.symbol,
                timestamp=stage_input.end_time,
                diagnostics={"error": "无回测结果"},
            )

        metrics = result.metrics
        summary = metrics_summary(metrics)

        # 多维度评分
        scores = self._evaluate_scores(metrics)

        return StageOutput(
            stage_name=self._name,
            symbol=stage_input.symbol,
            timestamp=stage_input.end_time,
            metrics={
                "correctness": scores["correctness"],
                "profitability": scores["profitability"],
                "drawdown_control": scores["drawdown_control"],
                "stability": scores["stability"],
                "robustness": scores["robustness"],
                "overall": scores["overall"],
            },
            data={
                "summary": summary,
                "backtest_result": result,  # 传递上游回测结果
            },
            diagnostics={
                "scores": scores,
                "evaluation_detail": summary,
            },
        )

    @staticmethod
    def _evaluate_scores(metrics: Any) -> dict[str, float]:
        """从多个维度评分，每项 0~10 分。"""
        scores: dict[str, float] = {}

        # 正确性：夏普比率是否为正
        scores["correctness"] = 5.0 + min(5.0, max(-5.0, metrics.sharpe_ratio * 2))

        # 盈利能力：年化收益
        ann_ret = getattr(metrics, "annualized_return", 0.0)
        scores["profitability"] = min(10.0, max(0.0, (ann_ret + 0.5) * 10))

        # 回撤控制
        dd = metrics.max_drawdown
        scores["drawdown_control"] = min(10.0, max(0.0, (1.0 - dd * 10)) * 10)

        # 稳定性：Calmar 比率
        calmar = getattr(metrics, "calmar_ratio", 0.0)
        scores["stability"] = min(10.0, max(0.0, calmar * 5))

        # 鲁棒性：交易次数和胜率
        trade_score = min(5.0, metrics.trade_count / 10)
        win_score = metrics.win_rate * 5
        scores["robustness"] = min(10.0, trade_score + win_score)

        # 综合
        scores["overall"] = (
            scores["correctness"] * 0.2
            + scores["profitability"] * 0.25
            + scores["drawdown_control"] * 0.2
            + scores["stability"] * 0.2
            + scores["robustness"] * 0.15
        )

        return scores


class ReportGenerationStage(PipelineStageProtocol):
    """阶段 5：报告生成。"""

    def __init__(self, strategy_name: str = "策略", name: str = "report") -> None:
        self._name = name
        self.strategy_name = strategy_name

    def stage_name(self) -> str:
        return self._name

    def execute(self, stage_input: StageInput) -> StageOutput:
        prev_data = stage_input.previous_output.get("data", {})
        result = prev_data.get("backtest_result")
        evaluation = stage_input.previous_output.get("metrics", {})

        if result is None:
            report_text = "无回测结果，无法生成报告。"
        else:
            report_text = BacktestReporter.text_report(
                result, self.strategy_name, detailed=True
            )

        return StageOutput(
            stage_name=self._name,
            symbol=stage_input.symbol,
            timestamp=stage_input.end_time,
            data={"report_text": report_text, "report_html": ""},
            metrics=evaluation,
            diagnostics={"report_length": len(report_text)},
        )


__all__ = [
    "DataLoadingStage",
    "StrategySignalStage",
    "BacktestExecutionStage",
    "PerformanceEvaluationStage",
    "ReportGenerationStage",
]