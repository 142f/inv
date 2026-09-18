"""标准化阶段流水线。

每个阶段职责独立、可单独运行，使用统一的结构化输入输出协议。
"""

from .base import PipelineStageProtocol, StageInput, StageOutput
from .stages import (
    DataLoadingStage,
    StrategySignalStage,
    BacktestExecutionStage,
    PerformanceEvaluationStage,
    ReportGenerationStage,
)
from .runner import PipelineRunner

__all__ = [
    "PipelineStageProtocol",
    "StageInput", "StageOutput",
    "DataLoadingStage",
    "StrategySignalStage",
    "BacktestExecutionStage",
    "PerformanceEvaluationStage",
    "ReportGenerationStage",
    "PipelineRunner",
]