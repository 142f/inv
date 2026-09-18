"""Pipeline 阶段基类与协议。

所有阶段继承 PipelineStageProtocol，使用统一的结构化输入输出。

StageInput / StageOutput 从 domain 层导入，保证全系统一致。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from inv.domain.models import StageInput, StageOutput


class PipelineStageProtocol(ABC):
    """Pipeline 阶段协议，所有阶段必须实现此接口。"""

    @abstractmethod
    def execute(self, stage_input: StageInput) -> StageOutput:
        """执行阶段逻辑。"""
        ...

    @abstractmethod
    def stage_name(self) -> str:
        """返回阶段名称。"""
        ...


__all__ = ["PipelineStageProtocol", "StageInput", "StageOutput"]