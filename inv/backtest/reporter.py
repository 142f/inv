"""回测结果报告生成器。"""

from __future__ import annotations

from statistics import fmean
from typing import Sequence

from inv.domain.models import BacktestResult, Trade
from inv.backtest.metrics import metrics_summary


class BacktestReporter:
    """回测报告生成器。"""

    @staticmethod
    def text_report(
        result: BacktestResult,
        strategy_name: str = "未知策略",
        detailed: bool = True,
    ) -> str:
        """生成文本格式的回测报告。"""
        m = result.metrics
        lines = [
            f"===== {strategy_name} 回测报告 =====",
            "",
            "--- 绩效指标 ---",
            metrics_summary(m),
        ]

        if detailed and result.trades:
            lines.extend([
                "",
                "--- 交易统计 ---",
                BacktestReporter._trade_stats(result.trades),
                "",
                "--- 诊断信息 ---",
                *[f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}"
                  for k, v in result.diagnostics.items()],
            ])

        lines.append("=" * 40)
        return "\n".join(lines)

    @staticmethod
    def compare_reports(
        results: dict[str, BacktestResult],
    ) -> str:
        """对比多个策略的回测结果。"""
        if not results:
            return "无结果可比较"

        header = f"{'策略':<20} {'收益':>10} {'夏普':>6} {'回撤':>7} {'胜率':>6} {'交易':>5}"
        sep = "-" * len(header)
        lines = ["多策略对比:", sep, header, sep]

        for name, result in sorted(
            results.items(),
            key=lambda item: item[1].metrics.sharpe_ratio,
            reverse=True,
        ):
            m = result.metrics
            lines.append(
                f"{name:<20} {m.net_profit:>10.2f} {m.sharpe_ratio:>6.2f} "
                f"{m.max_drawdown:>6.2%} {m.win_rate:>5.1%} {m.trade_count:>5}"
            )
        lines.extend([sep, f"总计: {len(results)} 个策略"])
        return "\n".join(lines)

    @staticmethod
    def _trade_stats(trades: Sequence[Trade]) -> str:
        """生成交易统计信息。"""
        if not trades:
            return "无交易记录"

        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        avg_win = fmean(wins) if wins else 0.0
        avg_loss = fmean(losses) if losses else 0.0
        max_win = max(wins) if wins else 0.0
        max_loss = min(losses) if losses else 0.0
        avg_bars_held = fmean([t.exit_time - t.entry_time for t in trades]) if trades else 0.0

        return (
            f"总交易次数: {len(trades)}\n"
            f"盈利交易: {len(wins)} ({len(wins)/len(trades)*100:.1f}%)\n"
            f"亏损交易: {len(losses)} ({len(losses)/len(trades)*100:.1f}%)\n"
            f"平均盈利: {avg_win:.2f}  |  最大盈利: {max_win:.2f}\n"
            f"平均亏损: {avg_loss:.2f}  |  最大亏损: {max_loss:.2f}\n"
            f"平均持有期: {avg_bars_held:.1f} 个周期"
        )


__all__ = ["BacktestReporter"]