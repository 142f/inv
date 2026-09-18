"""风险调整收益与交易质量指标。"""

from __future__ import annotations

from math import sqrt
from statistics import fmean, pstdev
from typing import Iterable

from core.domain import PerformanceMetrics

from .models import Trade


def calculate_metrics(
    equity_curve: Iterable[float],
    trades: Iterable[Trade],
    *,
    periods_per_year: int = 252,
    cvar_quantile: float = 0.05,
) -> PerformanceMetrics:
    equity = [float(item) for item in equity_curve]
    trade_list = list(trades)
    if not equity:
        equity = [0.0]
    pnl = [trade.net_pnl for trade in trade_list]
    returns = _returns(equity)
    volatility = pstdev(returns) * sqrt(periods_per_year) if len(returns) > 1 else 0.0
    mean_return = fmean(returns) if returns else 0.0
    stdev = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = (mean_return / stdev) * sqrt(periods_per_year) if stdev > 1e-12 else 0.0
    downside = [min(0.0, item) for item in returns]
    downside_dev = sqrt(fmean([item * item for item in downside])) if downside else 0.0
    sortino = (mean_return / downside_dev) * sqrt(periods_per_year) if downside_dev > 1e-12 else 0.0
    max_drawdown = _max_drawdown(equity)
    wins = [item for item in pnl if item > 0]
    losses = [item for item in pnl if item < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(pnl) if pnl else 0.0
    payoff = (fmean(wins) / abs(fmean(losses))) if wins and losses else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 1e-12 else (float("inf") if gross_profit > 0 else 0.0)
    net_profit = sum(pnl)
    turnover = sum(abs(trade.volume) for trade in trade_list)
    costs = sum(trade.cost for trade in trade_list)
    cost_ratio = costs / max(abs(net_profit) + costs, 1e-12)
    risk_reward = net_profit / max(max_drawdown, 1e-12) if max_drawdown > 0 else 0.0
    cvar = _cvar(returns, cvar_quantile)
    annualized_return = (
        (equity[-1] / equity[0]) ** (periods_per_year / max(len(equity) - 1, 1)) - 1.0
        if equity[0] > 0 and equity[-1] > 0 and len(equity) > 1
        else 0.0
    )
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
    )


def _returns(equity: list[float]) -> list[float]:
    return [
        (current - previous) / max(abs(previous), 1e-12)
        for previous, current in zip(equity, equity[1:])
    ]


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    maximum = 0.0
    for value in equity:
        peak = max(peak, value)
        maximum = max(maximum, (peak - value) / max(abs(peak), 1e-12))
    return maximum


def _cvar(returns: list[float], quantile: float) -> float:
    if not returns:
        return 0.0
    q = min(1.0, max(0.0, float(quantile)))
    count = max(1, int(len(returns) * q))
    worst = sorted(returns)[:count]
    return abs(fmean(worst))
