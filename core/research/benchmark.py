"""等风险买入持有基准，使用与网格策略相同的成本和波动目标。"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from .grid_backtest import _ewma_daily_volatility, _time
from .history import InstrumentSpec
from .metrics import calculate_metrics
from .models import BacktestResult, Bar, Trade
from .profiles import BacktestProfile, CostProfile, CostScenario


def _daily_closes(bars: Sequence[Bar]) -> tuple[tuple[datetime, float], ...]:
    values: list[tuple[datetime, float]] = []
    current_day = None
    last: Bar | None = None
    for bar in sorted(bars, key=lambda item: _time(item.timestamp)):
        stamp = _time(bar.timestamp)
        if current_day is not None and stamp.date() != current_day and last is not None:
            values.append((_time(last.timestamp), last.close))
        current_day = stamp.date()
        last = bar
    if last is not None:
        values.append((_time(last.timestamp), last.close))
    return tuple(values)


class EqualRiskBuyHoldBacktester:
    """每日仅在已收盘数据上重算目标仓位的多头基准。"""

    def __init__(self, profile: BacktestProfile, instrument: InstrumentSpec, cost: CostProfile) -> None:
        self.profile = profile
        self.instrument = instrument
        self.cost = cost
        self._periods_per_year = 365 if "BTC" in instrument.symbol.upper() else 252

    def run(self, bars: Sequence[Bar], *, scenario: CostScenario = CostScenario.BASELINE) -> BacktestResult:
        days = _daily_closes(bars)
        if len(days) < 2:
            raise ValueError("等风险基准至少需要两个完整交易日")
        equity = self.profile.initial_equity
        volume = 0.0
        prior_price = days[0][1]
        daily_equity = [equity]
        trades: list[Trade] = []
        historical_closes: list[float] = []
        cumulative_cost = 0.0
        cost_components = {"commission": 0.0, "slippage": 0.0, "swap": 0.0}

        for stamp, price in days:
            if historical_closes:
                funding = self._overnight_value(volume, prior_price, stamp.weekday(), scenario)
                equity += funding
                if funding:
                    fee = max(0.0, -funding)
                    cumulative_cost += fee
                    cost_components["swap"] += fee
                    trades.append(
                        Trade(
                            entry_time=stamp,
                            exit_time=stamp,
                            side="buy_hold_swap",
                            volume=0.0,
                            entry_price=prior_price,
                            exit_price=prior_price,
                            gross_pnl=max(0.0, funding),
                            cost=fee,
                        )
                    )
                gross = self.instrument.pnl(prior_price, price, volume, "buy")
                equity += gross
                trades.append(
                    Trade(
                        entry_time=stamp,
                        exit_time=stamp,
                        side="buy_hold_daily",
                        volume=volume,
                        entry_price=prior_price,
                        exit_price=price,
                        gross_pnl=gross,
                        cost=0.0,
                    )
                )
            volatility = _ewma_daily_volatility(
                historical_closes,
                self.profile.ewma_span_days,
                periods_per_year=self._periods_per_year,
            )
            ratio = self.profile.max_gross_notional_equity_ratio
            if volatility is not None and volatility > 1e-12:
                ratio = min(ratio, self.profile.target_annual_volatility / volatility)
            target_volume = equity * ratio / max(price * self.instrument.contract_size, 1e-12)
            # 买卖调仓都按相同单边成本计费；滑点以名义成本近似并明确记录在交易成本中。
            delta = abs(target_volume - volume)
            if delta > self.instrument.volume_step * 0.25:
                commission = self.cost.commission(price, delta, self.instrument.contract_size)
                slippage = self.cost.slippage(price, scenario) / max(price, 1e-12) * price * delta * self.instrument.contract_size
                rebalance_cost = commission + slippage
                equity -= rebalance_cost
                cumulative_cost += rebalance_cost
                cost_components["commission"] += commission
                cost_components["slippage"] += slippage
                trades.append(
                    Trade(
                        entry_time=stamp,
                        exit_time=stamp,
                        side="buy_hold_rebalance",
                        volume=delta,
                        entry_price=price,
                        exit_price=price,
                        gross_pnl=0.0,
                        cost=rebalance_cost,
                    )
                )
                volume = target_volume
            historical_closes.append(price)
            prior_price = price
            daily_equity.append(equity)

        # 最终平仓成本，避免与网格策略出现成本口径不一致。
        close_cost = self.cost.commission(prior_price, volume, self.instrument.contract_size)
        close_cost += self.cost.slippage(prior_price, scenario) * volume * self.instrument.contract_size
        equity -= close_cost
        cumulative_cost += close_cost
        cost_components["commission"] += self.cost.commission(prior_price, volume, self.instrument.contract_size)
        cost_components["slippage"] += self.cost.slippage(prior_price, scenario) * volume * self.instrument.contract_size
        trades.append(
            Trade(
                entry_time=days[-1][0],
                exit_time=days[-1][0],
                side="buy_hold_close",
                volume=0.0,
                entry_price=prior_price,
                exit_price=prior_price,
                gross_pnl=0.0,
                cost=close_cost,
            )
        )
        daily_equity[-1] = equity
        metrics = calculate_metrics(daily_equity, trades, periods_per_year=self._periods_per_year)
        return BacktestResult(
            equity_curve=tuple(daily_equity),
            trades=tuple(trades),
            metrics=metrics,
            diagnostics={
                "benchmark": "20 日 EWMA 波动目标的等风险买入持有",
                "costs": cost_components,
                "total_cost": cumulative_cost,
                "daily_observations": len(days),
            },
        )

    def _overnight_value(self, volume: float, price: float, weekday: int, scenario: CostScenario) -> float:
        multiplier = 3.0 if self.instrument.rollover3_weekday == weekday else 1.0
        if self.instrument.swap_long is not None and self.instrument.swap_mode == "points":
            return (
                self.instrument.swap_long
                * self.instrument.point
                / self.instrument.tick_size
                * self.instrument.tick_value
                * volume
                * multiplier
            )
        if self.instrument.swap_long is not None and self.instrument.swap_mode == "currency_deposit":
            return self.instrument.swap_long * volume * multiplier
        funding_bps = self.cost.fallback_funding_bps(scenario)
        if funding_bps is None:
            return 0.0
        return -price * volume * self.instrument.contract_size * funding_bps / 10_000.0 * multiplier
