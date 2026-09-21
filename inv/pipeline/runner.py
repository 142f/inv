"""Pipeline 运行器：按顺序执行各个阶段并传递数据。"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from inv.pipeline.base import PipelineStageProtocol, StageInput, StageOutput


class PipelineRunner:
    """Pipeline 运行器。

    按注册顺序依次执行各个阶段，前一个阶段的输出作为后一个阶段的输入。

    用法:
        runner = PipelineRunner()
        runner.add_stage(DataLoadingStage())
        runner.add_stage(StrategySignalStage(strategy, settings))
        final_output = runner.run(initial_input)
    """

    def __init__(self) -> None:
        self._stages: list[PipelineStageProtocol] = []

    def add_stage(self, stage: PipelineStageProtocol) -> "PipelineRunner":
        """添加一个阶段到流水线末尾。"""
        self._stages.append(stage)
        return self

    def add_stages(self, *stages: PipelineStageProtocol) -> "PipelineRunner":
        """批量添加阶段。"""
        self._stages.extend(stages)
        return self

    def insert_stage(
        self, index: int, stage: PipelineStageProtocol
    ) -> "PipelineRunner":
        """在指定位置插入阶段。"""
        self._stages.insert(index, stage)
        return self

    def remove_stage(self, stage_name: str) -> "PipelineRunner":
        """按名称移除阶段。"""
        self._stages = [s for s in self._stages if s.stage_name() != stage_name]
        return self

    @property
    def stage_names(self) -> list[str]:
        """返回所有阶段的名称列表。"""
        return [s.stage_name() for s in self._stages]

    def run(
        self,
        initial_input: StageInput,
        verbose: bool = False,
    ) -> StageOutput:
        """依次执行所有阶段。

        参数:
            initial_input: 初始输入
            verbose: 是否打印执行日志

        返回:
            最后一个阶段的输出
        """
        if not self._stages:
            raise ValueError("Pipeline 至少需要一个执行阶段")

        current_input = initial_input

        for stage in self._stages:
            name = stage.stage_name()
            if verbose:
                print(f"[Pipeline] 执行阶段: {name}")

            output = stage.execute(current_input)

            if verbose:
                metrics_str = (
                    ", ".join(
                        f"{k}={v:.4f}"
                        for k, v in output.metrics.items()
                    )
                    if output.metrics
                    else "无指标"
                )
                print(f"  -> 完成: {metrics_str}")

            # 将当前输出作为下一个阶段的输入
            current_input = StageInput(
                symbol=output.symbol,
                bars=current_input.bars,
                start_time=current_input.start_time,
                end_time=output.timestamp,
                config=current_input.config,
                previous_output={
                    "data": output.data,
                    "metrics": output.metrics,
                    "diagnostics": output.diagnostics,
                    "signals": output.signals,
                },
            )

        return output

    def run_segment(
        self,
        initial_input: StageInput,
        start_stage: int = 0,
        end_stage: int | None = None,
    ) -> StageOutput:
        """运行流水线的一个片段。"""
        end = end_stage if end_stage is not None else len(self._stages)
        subset = self._stages[start_stage:end]
        saved = self._stages
        self._stages = list(subset)
        try:
            return self.run(initial_input)
        finally:
            self._stages = saved


__all__ = ["PipelineRunner"]
