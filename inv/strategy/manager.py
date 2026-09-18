"""策略管理器：注册、查找、生命周期管理。"""

from __future__ import annotations

from typing import Any

from inv.strategy.base import BaseStrategy
from inv.strategy.momentum import MomentumStrategy
from inv.strategy.mean_reversion import MeanReversionStrategy
from inv.strategy.breakout import BreakoutStrategy
from inv.strategy.grid_planner import GridPlanner


class StrategyManager:
    """策略管理器，负责策略的注册和创建。"""

    _registry: dict[str, type[BaseStrategy]] = {}

    @classmethod
    def register(cls, strategy_id: str, strategy_cls: type[BaseStrategy]) -> None:
        """注册策略类型。"""
        cls._registry[strategy_id] = strategy_cls

    @classmethod
    def create(cls, strategy_id: str, symbol: str, **kwargs: Any) -> BaseStrategy:
        """创建策略实例。"""
        if strategy_id in cls._registry:
            return cls._registry[strategy_id](strategy_id, symbol, **kwargs)
        raise KeyError(f"未注册的策略类型: {strategy_id}")

    @classmethod
    def list_available(cls) -> list[str]:
        """列出所有已注册的策略。"""
        return list(cls._registry.keys())

    @classmethod
    def get_strategy_class(cls, strategy_id: str) -> type[BaseStrategy] | None:
        return cls._registry.get(strategy_id)


# 注册内置策略
StrategyManager.register("momentum", MomentumStrategy)
StrategyManager.register("mean_reversion", MeanReversionStrategy)
StrategyManager.register("breakout", BreakoutStrategy)
StrategyManager.register("grid", GridPlanner)

# 别名
StrategyManager.register("trend", MomentumStrategy)
StrategyManager.register("bollinger", MeanReversionStrategy)
StrategyManager.register("donchian", BreakoutStrategy)
StrategyManager.register("grid_trading", GridPlanner)


__all__ = ["StrategyManager"]