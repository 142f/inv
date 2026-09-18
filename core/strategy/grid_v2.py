"""无 MT5 依赖的网格策略规划器。

该模块是新策略内核；旧 mixin 仅作为兼容适配层保留，不能在此模块中产生 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core.domain import CommandAction, MarketSnapshot, OrderCommand, StrategyDecision, StrategySettings, SymbolSpec


@dataclass(frozen=True, slots=True)
class GridState:
    anchor_ticks: int | None = None
    last_regime: str = "normal"


@dataclass(frozen=True, slots=True)
class Inventory:
    long_volume: float
    short_volume: float
    pending_buy_volume: float
    pending_sell_volume: float

    @property
    def net_volume(self) -> float:
        return self.long_volume + self.pending_buy_volume - self.short_volume - self.pending_sell_volume

    @property
    def gross_volume(self) -> float:
        return self.long_volume + self.short_volume + self.pending_buy_volume + self.pending_sell_volume


class InventoryPolicy:
    """根据当前净敞口收缩风险方向的网格窗口。"""

    @staticmethod
    def windows(base_buy: int, base_sell: int, net_volume: float, max_net_volume: float | None) -> tuple[int, int, float]:
        if max_net_volume is None or max_net_volume <= 0:
            return max(0, base_buy), max(0, base_sell), 0.0
        pressure = max(-1.0, min(1.0, net_volume / max_net_volume))
        buy_scale = max(0.0, 1.0 - pressure) if pressure >= 0 else min(2.0, 1.0 - pressure * 0.5)
        sell_scale = max(0.0, 1.0 + pressure) if pressure <= 0 else min(2.0, 1.0 + pressure * 0.5)
        return int(round(base_buy * buy_scale)), int(round(base_sell * sell_scale)), pressure


class RegimePolicy:
    """使用外部提供的波动状态，不在策略内请求行情。"""

    @staticmethod
    def classify(*, spread: float, atr: float | None, maximum_relative_spread: float = 0.35) -> str:
        if atr is not None and atr > 0 and spread / atr >= maximum_relative_spread:
            return "stressed"
        return "normal"


class ExitPolicy:
    """计算每张网格订单的保护价格。"""

    @staticmethod
    def protection(*, side: str, entry_ticks: int, tp_ticks: int, sl_ticks: int | None) -> Mapping[str, int]:
        is_buy = side == "buy"
        payload: dict[str, int] = {
            "tp_ticks": entry_ticks + tp_ticks if is_buy else entry_ticks - tp_ticks,
        }
        if sl_ticks is not None and sl_ticks > 0:
            payload["sl_ticks"] = entry_ticks - sl_ticks if is_buy else entry_ticks + sl_ticks
        return payload


class HedgePlanner:
    """多空对称对冲规划；只在净暴露超过预算时生成降风险市价命令。"""

    @staticmethod
    def plan(
        *,
        strategy_id: str,
        symbol: str,
        inventory: Inventory,
        max_net_volume: float | None,
        fraction: float,
        volume_step: float,
    ) -> tuple[OrderCommand, ...]:
        if max_net_volume is None or max_net_volume <= 0:
            return ()
        net = inventory.net_volume
        if abs(net) <= max_net_volume:
            return ()
        hedge_volume = min(abs(net) - max_net_volume, abs(net) * max(0.0, min(1.0, fraction)))
        steps = int(hedge_volume / max(volume_step, 1e-12))
        hedge_volume = steps * volume_step
        if hedge_volume <= 0:
            return ()
        side = "sell" if net > 0 else "buy"
        key = f"{strategy_id}:hedge:{side}:{hedge_volume:.8f}"
        return (
            OrderCommand(
                idempotency_key=key,
                action=CommandAction.OPEN_HEDGE,
                strategy_id=strategy_id,
                symbol=symbol,
                payload={"side": side, "volume": hedge_volume, "reduce_only": True},
                priority=10,
            ),
        )


class GridPlanner:
    """生成网格目标与订单意图；价格比较全程使用整数 tick。"""

    def plan(
        self,
        *,
        settings: StrategySettings,
        symbol: SymbolSpec,
        snapshot: MarketSnapshot,
        state: GridState,
        inventory: Inventory,
    ) -> tuple[StrategyDecision, GridState]:
        tick = snapshot.tick
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        if not settings.enabled or bid <= 0 or ask <= 0 or ask < bid:
            return StrategyDecision(settings.strategy_id, diagnostics={"reason": "报价无效或策略禁用"}), state

        spread = ask - bid
        regime = RegimePolicy.classify(spread=spread, atr=snapshot.atr)
        if regime == "stressed":
            return StrategyDecision(settings.strategy_id, diagnostics={"reason": "异常点差/波动状态"}), GridState(state.anchor_ticks, regime)

        mid_ticks = symbol.price_to_ticks((bid + ask) * 0.5)
        anchor = state.anchor_ticks if state.anchor_ticks is not None else mid_ticks
        step_ticks = max(1, symbol.price_to_ticks(settings.step))
        tp_ticks = max(1, symbol.price_to_ticks(settings.tp_dist))
        sl_value = settings.legacy.get("sl_dist", 0.0)
        sl_ticks = symbol.price_to_ticks(float(sl_value)) if sl_value else None
        mode = str(settings.legacy.get("mode", "neutral") or "neutral").lower()
        base_window = max(0, int(settings.legacy.get("window", 6) or 0))
        buy_base = max(0, int(settings.legacy.get("buy_window", base_window) or 0))
        sell_base = max(0, int(settings.legacy.get("sell_window", base_window) or 0))
        buy_window, sell_window, pressure = InventoryPolicy.windows(
            buy_base, sell_base, inventory.net_volume, settings.risk.max_net_volume
        )
        if mode == "long":
            sell_window = 0
        elif mode == "short":
            buy_window = 0

        min_ticks = symbol.price_to_ticks(float(settings.legacy.get("min_p", 0.0) or 0.0))
        max_ticks = symbol.price_to_ticks(float(settings.legacy.get("max_p", float("inf")))) if settings.legacy.get("max_p") else 2**62
        bid_ticks = symbol.price_to_ticks(bid)
        ask_ticks = symbol.price_to_ticks(ask)
        lot = symbol.normalize_volume(settings.lot)
        commands: list[OrderCommand] = []

        for side, window, direction, market_ticks in (
            ("buy", buy_window, -1, ask_ticks),
            ("sell", sell_window, 1, bid_ticks),
        ):
            for index in range(1, window + 1):
                # 第一层距离锚点恰为一个 ATR 步长，避免把首层意外放在两倍步长。
                price_ticks = anchor + direction * index * step_ticks
                if price_ticks < min_ticks or price_ticks > max_ticks:
                    continue
                if (side == "buy" and price_ticks >= market_ticks) or (side == "sell" and price_ticks <= market_ticks):
                    continue
                protection = ExitPolicy.protection(
                    side=side,
                    entry_ticks=price_ticks,
                    tp_ticks=tp_ticks,
                    sl_ticks=sl_ticks,
                )
                key = f"{settings.strategy_id}:limit:{side}:{price_ticks}:{lot:.8f}"
                commands.append(
                    OrderCommand(
                        idempotency_key=key,
                        action=CommandAction.PLACE_LIMIT,
                        strategy_id=settings.strategy_id,
                        symbol=settings.symbol,
                        payload={
                            "side": side,
                            "price_ticks": price_ticks,
                            "volume": lot,
                            **protection,
                        },
                        priority=50 + index,
                    )
                )

        commands.extend(
            HedgePlanner.plan(
                strategy_id=settings.strategy_id,
                symbol=settings.symbol,
                inventory=inventory,
                max_net_volume=settings.risk.max_net_volume,
                fraction=float(settings.legacy.get("hedge_fraction", 0.3333) or 0.3333),
                volume_step=symbol.volume_step,
            )
        )
        diagnostics = {"inventory_pressure": pressure, "regime": regime, "anchor_ticks": anchor}
        return StrategyDecision(settings.strategy_id, tuple(commands), diagnostics), GridState(anchor, regime)
