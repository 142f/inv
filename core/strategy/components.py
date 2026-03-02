"""Strategy components: grid calculator and risk manager."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Set, Tuple


# ── Grid Calculator ───────────────────────────────────────────────────────────

class GridCalculator:
    # Safety guard for unexpected parameter combinations.
    _MAX_ITERATIONS = 10000

    def __init__(self, normalize_price: Callable[[float], float]):
        self._normalize_price = normalize_price

    def build_targets(
        self,
        *,
        anchor: float,
        step: float,
        min_price: float,
        max_price: float,
        bid: float,
        ask: float,
        buy_window: int,
        sell_window: int,
        mode: str,
        recenter_steps: int,
        min_dist: Optional[float] = None,
        blocked_k: Optional[Set[int]] = None,
    ) -> Tuple[List[float], List[float]]:
        # Keep API compatibility: recenter_steps is intentionally unused here.
        _ = recenter_steps

        target_buys: List[float] = []
        target_sells: List[float] = []

        if step <= 0:
            return target_buys, target_sells
        if max_price <= min_price:
            return target_buys, target_sells
        if bid <= 0 or ask <= 0 or ask < bid:
            return target_buys, target_sells

        blocked_levels = blocked_k or set()
        safe_min_dist = max(0.0, float(min_dist)) if min_dist is not None else 0.0

        if mode in ("neutral", "long") and buy_window > 0:
            start_k = int((ask - anchor) / step) + 3
            target_buys = self._collect_side_targets(
                side="buy",
                start_k=start_k,
                k_delta=-1,
                window=buy_window,
                anchor=anchor,
                step=step,
                min_price=min_price,
                max_price=max_price,
                bid=bid,
                ask=ask,
                min_dist=safe_min_dist,
                blocked_k=blocked_levels,
            )

        if mode in ("neutral", "short") and sell_window > 0:
            start_k = int((bid - anchor) / step) - 3
            target_sells = self._collect_side_targets(
                side="sell",
                start_k=start_k,
                k_delta=1,
                window=sell_window,
                anchor=anchor,
                step=step,
                min_price=min_price,
                max_price=max_price,
                bid=bid,
                ask=ask,
                min_dist=safe_min_dist,
                blocked_k=blocked_levels,
            )
            target_sells.sort(reverse=True)

        return target_buys, target_sells

    def _collect_side_targets(
        self,
        *,
        side: str,
        start_k: int,
        k_delta: int,
        window: int,
        anchor: float,
        step: float,
        min_price: float,
        max_price: float,
        bid: float,
        ask: float,
        min_dist: float,
        blocked_k: Set[int],
    ) -> List[float]:
        targets: List[float] = []
        k = start_k
        iterations = 0

        while len(targets) < window and iterations < self._MAX_ITERATIONS:
            iterations += 1
            level = self._normalize_price(anchor + k * step)

            if side == "buy":
                if level < min_price - step:
                    break
                is_candidate = level < ask and min_price <= level <= max_price
            else:
                if level > max_price + step:
                    break
                is_candidate = level > bid and min_price <= level <= max_price

            if is_candidate and not self._should_skip_level(
                side=side,
                level=level,
                anchor=anchor,
                step=step,
                bid=bid,
                ask=ask,
                min_dist=min_dist,
                blocked_k=blocked_k,
            ):
                targets.append(level)

            k += k_delta

        return targets

    @staticmethod
    def _should_skip_level(
        *,
        side: str,
        level: float,
        anchor: float,
        step: float,
        bid: float,
        ask: float,
        min_dist: float,
        blocked_k: Set[int],
    ) -> bool:
        if min_dist > 0.0:
            if side == "buy" and (ask - level) < min_dist:
                return True
            if side == "sell" and (level - bid) < min_dist:
                return True

        if blocked_k:
            level_k = round((level - anchor) / step)
            if level_k in blocked_k:
                return True

        return False


# ── Risk Manager ──────────────────────────────────────────────────────────────

class RangeAction(Enum):
    """价格范围检查的结果动作。"""
    OK = "ok"
    FREEZE = "freeze"
    STOP = "stop"


@dataclass
class SpreadCheck:
    """点差检查结果。"""
    triggered: bool
    spread: float
    pause_until: float


class RiskManager:
    """风险管理组件，负责各类风控检查。"""
    EPSILON = 1e-9

    def check_spread(
        self,
        *,
        bid: float,
        ask: float,
        max_spread_points: Optional[float],
        point: float,
        extreme_cooldown: float,
        now: float,
    ) -> SpreadCheck:
        """检查当前点差是否超过阈值。"""
        if max_spread_points is None or max_spread_points <= 0:
            return SpreadCheck(False, 0.0, 0.0)

        if point <= 0:
            return SpreadCheck(False, 0.0, 0.0)

        spread = ask - bid
        # 边界检查：点差不应为负
        if spread < 0:
            return SpreadCheck(True, spread, now + extreme_cooldown)
            
        if spread > max_spread_points * point:
            return SpreadCheck(True, spread, now + extreme_cooldown)
        return SpreadCheck(False, spread, 0.0)

    def check_range(
        self,
        *,
        mid_price: float,
        min_price: float,
        max_price: float,
        out_of_range_action: str,
    ) -> RangeAction:
        """检查当前价格是否在有效范围内。"""
        # 参数校验
        if min_price >= max_price:
            # 无效的价格范围配置，保守处理为冻结
            return RangeAction.FREEZE
            
        if mid_price < min_price or mid_price > max_price:
            action_str = str(out_of_range_action).lower().strip()
            if action_str == "stop":
                return RangeAction.STOP
            # 默认为 freeze
            return RangeAction.FREEZE
            
        return RangeAction.OK

    def check_inventory_limits(
        self,
        *,
        long_vol: float,
        short_vol: float,
        pending_buy_vol: float,
        pending_sell_vol: float,
        net_vol: Optional[float] = None,
        lot: float,
        side: str,
        mode: str,
        max_net_vol: Optional[float] = None,
        max_long_vol: Optional[float] = None,
        max_short_vol: Optional[float] = None,
        max_long_pos: Optional[int] = None,
        max_short_pos: Optional[int] = None,
        long_pos_count: int = 0,
        short_pos_count: int = 0,
        hedge_enabled: bool = False,
    ) -> bool:
        """Return whether a new order is allowed under inventory constraints."""
        if lot <= 0:
            return False

        eps = self.EPSILON
        effective_net_vol = net_vol
        if effective_net_vol is None:
            effective_net_vol = (long_vol + pending_buy_vol) - (short_vol + pending_sell_vol)
        
        # 单边持仓量限制检查
        if side == "buy":
            if max_long_vol is not None:
                if (long_vol + pending_buy_vol + lot) > max_long_vol + eps:
                    return False
            if max_long_pos is not None:
                if (long_pos_count + 1) > max_long_pos:
                    return False
        else:  # sell
            if max_short_vol is not None:
                if (short_vol + pending_sell_vol + lot) > max_short_vol + eps:
                    return False
            if max_short_pos is not None:
                if (short_pos_count + 1) > max_short_pos:
                    return False
        
        # 净持仓量限制检查
        if max_net_vol is None:
            return True

        cap = float(max_net_vol)

        if mode == "neutral":
            new_net = effective_net_vol + lot if side == "buy" else effective_net_vol - lot
            # 如果当前已超限，只允许减少绝对值的操作
            if abs(effective_net_vol) > cap + eps:
                return abs(new_net) < abs(effective_net_vol) - eps
            return abs(new_net) <= cap + eps

        if mode == "long":
            total_long = long_vol + pending_buy_vol
            if side == "buy":
                return (total_long + lot) <= cap + eps
            else:
                # 卖单会减少净多头，检查是否会变成净空头
                new_net = effective_net_vol - lot
                if not hedge_enabled:
                    return False
                if new_net < -eps:
                    return False
                return True

        if mode == "short":
            total_short = short_vol + pending_sell_vol
            if side == "sell":
                return (total_short + lot) <= cap + eps
            else:
                # 买单会增加净多头，检查是否会变成净多头
                new_net = effective_net_vol + lot
                if not hedge_enabled:
                    return False
                if new_net > eps:
                    return False
                return True

        return True
