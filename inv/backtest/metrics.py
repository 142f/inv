"""风险调整收益与交易质量指标计算。

与 core/research/metrics.py 功能一致，但直接使用新的领域模型。
"""

from __future__ import annotations

from math import sqrt
from statistics import fmean, pstdev
from typing import Iterable
import numpy as np

from inv.domain.models import PerformanceMetrics, Trade


def calculate_metrics(
    equity_curve: Iterable[float],
    trades: Iterable[Trade],
    *,
    periods_per_year: int = 252,
    cvar_quantile: float = 0.05,
) -> PerformanceMetrics:
    """计算完整的绩效指标集合。

    参数:
        equity_curve: 权益曲线（每个时间点的总权益）
        trades: 交易记录列表
        periods_per_year: 年化周期数（日线=252，小时=252*24）
        cvar_quantile: CVaR 分位数

    返回:
        PerformanceMetrics 对象
    """
    equity = [float(item) for item in equity_curve]
    trade_list = list(trades)
    if not equity:
        equity = [0.0]
    pnl = [trade.net_pnl for trade in trade_list]
    returns = _returns(equity)

    # 波动率
    volatility = pstdev(returns) * sqrt(periods_per_year) if len(returns) > 1 else 0.0

    # 收益率统计
    mean_return = fmean(returns) if returns else 0.0
    stdev = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = (mean_return / stdev) * sqrt(periods_per_year) if stdev > 1e-12 else 0.0

    # Sortino
    downside = [min(0.0, item) for item in returns]
    downside_dev = sqrt(fmean([item * item for item in downside])) if downside else 0.0
    sortino = (mean_return / downside_dev) * sqrt(periods_per_year) if downside_dev > 1e-12 else 0.0

    # 最大回撤
    max_drawdown = _max_drawdown(equity)

    # 胜率统计
    wins = [item for item in pnl if item > 0]
    losses = [item for item in pnl if item < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(pnl) if pnl else 0.0
    payoff = (fmean(wins) / abs(fmean(losses))) if wins and losses else 0.0
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 1e-12 else (float("inf") if gross_profit > 0 else 0.0)
    )

    net_profit = sum(pnl)
    turnover = sum(abs(trade.volume) for trade in trade_list)
    costs = sum(trade.cost for trade in trade_list)
    cost_ratio = costs / max(abs(net_profit) + costs, 1e-12)
    # 统一为无量纲收益/回撤，避免同策略仅因初始资金不同改变排序。
    total_return = equity[-1]/equity[0]-1 if equity[0] > 0 else 0.0
    risk_reward = total_return / max_drawdown if max_drawdown > 0 else 0.0

    # CVaR
    cvar = _cvar(returns, cvar_quantile)

    # 年化收益
    annualized_return = (
        (equity[-1] / equity[0]) ** (periods_per_year / max(len(equity) - 1, 1)) - 1.0
        if equity[0] > 0 and equity[-1] > 0 and len(equity) > 1
        else (-1.0 if equity[0] > 0 and equity[-1] <= 0 else 0.0)
    )

    # Calmar 比率
    calmar = annualized_return / max(max_drawdown, 1e-12) if max_drawdown > 0 else 0.0

    return PerformanceMetrics(
        net_profit=net_profit,
        volatility=volatility,
        max_drawdown=max_drawdown,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        win_rate=win_rate,
        payoff_ratio=payoff,
        profit_factor=profit_factor,
        risk_reward_ratio=risk_reward,
        trade_count=len(trade_list),
        turnover=turnover,
        cost_ratio=cost_ratio,
        cvar=cvar,
        annualized_return=annualized_return,
        calmar_ratio=calmar,
    )


def _returns(equity: list[float]) -> list[float]:
    """计算收益率序列。"""
    return [
        (current - previous) / max(abs(previous), 1e-12)
        for previous, current in zip(equity, equity[1:])
    ]


def _max_drawdown(equity: list[float]) -> float:
    """计算最大回撤。"""
    peak = equity[0]
    maximum = 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = (peak - value) / max(abs(peak), 1e-12)
        maximum = max(maximum, drawdown)
    return maximum


def _cvar(returns: list[float], quantile: float) -> float:
    """计算条件 VaR（Expected Shortfall）。"""
    if not returns:
        return 0.0
    q = min(1.0, max(0.0, float(quantile)))
    count = max(1, int(len(returns) * q))
    if len(returns) < 2048:
        return abs(fmean(sorted(returns)[:count]))
    # 只需最差尾部样本，无需 O(n log n) 全排序。
    values = np.asarray(returns, dtype=float)
    values.partition(count - 1)
    worst = values[:count]
    return abs(float(np.mean(worst)))


def metrics_summary(metrics: PerformanceMetrics) -> str:
    """生成可读的绩效摘要。"""
    return (
        f"净收益: {metrics.net_profit:>10.2f}  "
        f"年化收益: {metrics.annualized_return:>6.2%}  "
        f"最大回撤: {metrics.max_drawdown:>6.2%}  "
        f"夏普: {metrics.sharpe_ratio:>6.2f}  "
        f"索提诺: {metrics.sortino_ratio:>6.2f}  "
        f"Calmar: {metrics.calmar_ratio:>6.2f}\n"
        f"胜率: {metrics.win_rate:>6.2%}  "
        f"盈亏比: {metrics.payoff_ratio:>6.2f}  "
        f"利润因子: {metrics.profit_factor:>6.2f}  "
        f"交易次数: {metrics.trade_count:>6d}  "
        f"成本占比: {metrics.cost_ratio:>6.2%}  "
        f"CVaR: {metrics.cvar:>6.2%}"
    )


__all__ = ["calculate_metrics", "metrics_summary"]
