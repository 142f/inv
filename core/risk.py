"""与券商无关的策略级和组合级风险控制。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from core.domain import RiskDecision, RiskSettings


@dataclass(slots=True)
class RiskSnapshot:
    """单个策略在一个周期内可观测的风险输入。"""

    strategy_id: str
    now: float
    equity: float | None
    balance: float | None
    margin_level: float | None
    bid: float | None
    ask: float | None
    tick_time: float | None = None
    point: float = 1.0
    contract_size: float = 1.0
    positions: Iterable[Any] = ()
    orders: Iterable[Any] = ()


@dataclass(slots=True)
class _RiskState:
    high_watermark: float | None = None
    day: date | None = None
    day_start_equity: float | None = None


def _position_volume(position: Any) -> float:
    return max(0.0, float(getattr(position, "volume", 0.0) or 0.0))


def _position_sign(position: Any) -> float:
    """MT5 的 BUY/SELL 枚举分别为 0/1；适配器也可提供 direction。"""
    direction = str(getattr(position, "direction", "")).lower()
    if direction in {"buy", "long"}:
        return 1.0
    if direction in {"sell", "short"}:
        return -1.0
    return 1.0 if int(getattr(position, "type", 0) or 0) == 0 else -1.0


class RiskCoordinator:
    """在策略执行前给出确定性的允许或降风险决策。"""

    def __init__(self) -> None:
        self._states: dict[str, _RiskState] = {}

    def assess(self, snapshot: RiskSnapshot, settings: RiskSettings) -> RiskDecision:
        reasons: list[str] = []
        state = self._states.setdefault(snapshot.strategy_id, _RiskState())
        equity = self._positive_or_none(snapshot.equity)
        today = date.fromtimestamp(snapshot.now)

        if equity is not None:
            if state.day != today:
                state.day = today
                state.day_start_equity = equity
            state.high_watermark = max(state.high_watermark or equity, equity)

        if settings.max_tick_age_seconds is not None and settings.max_tick_age_seconds > 0 and snapshot.tick_time is not None:
            age = max(0.0, float(snapshot.now) - float(snapshot.tick_time))
            if age > settings.max_tick_age_seconds:
                reasons.append("行情时间戳过期")

        if snapshot.bid is None or snapshot.ask is None or snapshot.bid <= 0 or snapshot.ask < snapshot.bid:
            reasons.append("行情报价无效")
        elif settings.max_spread_points is not None:
            # 此处以价格点为单位；接入层应将 point 换算后的阈值放入配置。
            if (snapshot.ask - snapshot.bid) > settings.max_spread_points * max(snapshot.point, 1e-12):
                reasons.append("点差超过风险预算")

        if settings.min_margin_level is not None and snapshot.margin_level is not None:
            if float(snapshot.margin_level) < settings.min_margin_level:
                reasons.append("保证金水平低于阈值")

        long_volume, short_volume = self._position_exposure(snapshot.positions)
        pending_long, pending_short = self._order_exposure(snapshot.orders)
        long_volume += pending_long
        short_volume += pending_short
        gross_volume = long_volume + short_volume
        net_volume = long_volume - short_volume
        if settings.max_net_volume is not None and abs(net_volume) > settings.max_net_volume:
            reasons.append("净敞口超过风险预算")
        if settings.max_gross_volume is not None and gross_volume > settings.max_gross_volume:
            reasons.append("总敞口超过风险预算")
        if settings.max_notional is not None:
            reference_price = max(float(snapshot.bid or 0.0), float(snapshot.ask or 0.0))
            notional = gross_volume * reference_price * max(float(snapshot.contract_size), 1.0)
            if notional > settings.max_notional:
                reasons.append("名义敞口超过风险预算")
        if settings.max_open_orders is not None and len(tuple(snapshot.orders)) > settings.max_open_orders:
            reasons.append("挂单数量超过风险预算")

        if equity is not None and state.high_watermark and settings.max_drawdown_ratio is not None:
            drawdown = (state.high_watermark - equity) / max(state.high_watermark, 1e-12)
            if drawdown >= settings.max_drawdown_ratio:
                reasons.append("权益回撤超过风险预算")

        if equity is not None and state.day_start_equity and settings.max_daily_loss is not None:
            daily_loss = state.day_start_equity - equity
            if daily_loss >= settings.max_daily_loss:
                reasons.append("当日亏损超过风险预算")

        if not reasons:
            return RiskDecision(allowed=True)
        return RiskDecision(
            allowed=False,
            halt=True,
            cancel_pending=True,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _positive_or_none(value: float | None) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None

    @staticmethod
    def _position_exposure(positions: Iterable[Any]) -> tuple[float, float]:
        long_volume = 0.0
        short_volume = 0.0
        for position in positions:
            if _position_sign(position) > 0:
                long_volume += _position_volume(position)
            else:
                short_volume += _position_volume(position)
        return long_volume, short_volume

    @staticmethod
    def _order_exposure(orders: Iterable[Any]) -> tuple[float, float]:
        long_volume = 0.0
        short_volume = 0.0
        for order in orders:
            volume = max(
                0.0,
                float(getattr(order, "volume_current", getattr(order, "volume_initial", 0.0)) or 0.0),
            )
            direction = str(getattr(order, "direction", "")).lower()
            order_type = int(getattr(order, "type", -1) or -1)
            if direction in {"buy", "long"} or order_type in {0, 2, 4, 6}:
                long_volume += volume
            elif direction in {"sell", "short"} or order_type in {1, 3, 5, 7}:
                short_volume += volume
        return long_volume, short_volume
