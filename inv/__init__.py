"""量化研究与回测系统 v2.0

目录结构:
    domain/         领域模型、枚举、协议与配置
    strategy/       策略框架与多策略实现（动量、均值回归、突破、网格）
    backtest/       严格时间序列回测引擎、参数优化器、Walk-Forward 验证
    pipeline/       标准化阶段流水线（数据加载→信号→回测→评估→报告）
    broker/         券商适配器接口
    data/           行情数据提供商
    engine/         引擎调度与风控

核心原则：
    1. 禁止未来数据 —— 每步仅使用已闭合 K 线
    2. 严格时间推进 —— 当前时间点已有数据 → 计算 → 信号 → 执行 → 推进
    3. Stage 标准化 —— 每个阶段职责独立，输入输出统一
    4. 参数实验 —— 20+ 组参数组合，Walk-Forward 验证
"""

__version__ = "2.0.0"

# 领域层（核心）
from inv.domain import (
    Bar, Tick, Signal, SignalType,
    Trade, PerformanceMetrics, BacktestResult,
    SymbolSpec, OrderCommand, StrategyDecision,
    RiskDecision, CommandResult,
    StrategySettings, RiskSettings,
    BrokerProtocol, DataProviderProtocol,
    StrategyProtocol, PipelineStageProtocol,
)

# 策略
from inv.strategy import (
    BaseStrategy, StrategyManager,
    MomentumStrategy, MeanReversionStrategy,
    BreakoutStrategy, GridPlanner,
)

# 回测
from inv.backtest import (
    BacktestEngine, ParameterOptimizer,
    WalkForwardOptimizer, ParameterGrid,
    OptimizationResult, find_best_params,
    params_summary, calculate_metrics, BacktestReporter,
)

# Pipeline
from inv.pipeline import (
    PipelineRunner,
    DataLoadingStage, StrategySignalStage,
    BacktestExecutionStage, PerformanceEvaluationStage,
    ReportGenerationStage,
)

__all__ = [
    # 领域
    "Bar", "Tick", "Signal", "SignalType",
    "Trade", "PerformanceMetrics", "BacktestResult",
    "SymbolSpec", "OrderCommand", "StrategyDecision",
    "RiskDecision", "CommandResult",
    "StrategySettings", "RiskSettings",
    "BrokerProtocol", "DataProviderProtocol",
    "StrategyProtocol", "PipelineStageProtocol",
    # 策略
    "BaseStrategy", "StrategyManager",
    "MomentumStrategy", "MeanReversionStrategy",
    "BreakoutStrategy", "GridPlanner",
    # 回测
    "BacktestEngine", "ParameterOptimizer",
    "WalkForwardOptimizer", "ParameterGrid",
    "calculate_metrics", "BacktestReporter",
    # Pipeline
    "PipelineRunner",
    "DataLoadingStage", "StrategySignalStage",
    "BacktestExecutionStage", "PerformanceEvaluationStage",
    "ReportGenerationStage",
]