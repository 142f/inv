from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Set, Tuple


# ── Grid Calculator ───────────────────────────────────────────────────────────

class GridCalculator:
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
        min_dist: Optional[float] = None,
        blocked_k: Optional[Set[int]] = None,
    ) -> Tuple[List[float], List[float]]:
        target_buys: List[float] = []
        target_sells: List[float] = []

        if step <= 0 or max_price <= min_price or bid <= 0 or ask <= 0 or ask < bid:
            return target_buys, target_sells

        blocked_levels = blocked_k or set()
        safe_min_dist = max(0.0, float(min_dist)) if min_dist is not None else 0.0

        if mode in ("neutral", "long") and buy_window > 0:
            start_k = int((ask - anchor) / step) + 3
            target_buys = self._collect_side_targets(
                side="buy", start_k=start_k, k_delta=-1, window=buy_window,
                anchor=anchor, step=step, min_price=min_price, max_price=max_price,
                bid=bid, ask=ask, min_dist=safe_min_dist, blocked_k=blocked_levels,
            )

        if mode in ("neutral", "short") and sell_window > 0:
            start_k = int((bid - anchor) / step) - 3
            target_sells = self._collect_side_targets(
                side="sell", start_k=start_k, k_delta=1, window=sell_window,
                anchor=anchor, step=step, min_price=min_price, max_price=max_price,
                bid=bid, ask=ask, min_dist=safe_min_dist, blocked_k=blocked_levels,
            )
            # ✨ 关键修改点 1：因 target_sells 生成时已严格单调递增，使用 reverse() 替代 sort()
            target_sells.reverse()

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
        
        # ✨ 关键修改点 2：循环不变式外提，避免高频循环内重复进行字符串比对
        is_buy = side == "buy"

        while len(targets) < window and iterations < self._MAX_ITERATIONS:
            iterations += 1
            level = self._normalize_price(anchor + k * step)

            # ✨ 关键修改点 3：内联 _should_skip_level 的逻辑，消除函数调用开销与二次 side 判断
            if is_buy:
                if level < min_price - step:
                    break
                is_candidate = level < ask and min_price <= level <= max_price
                if is_candidate and min_dist > 0.0 and (ask - level) < min_dist:
                    is_candidate = False
            else:
                if level > max_price + step:
                    break
                is_candidate = level > bid and min_price <= level <= max_price
                if is_candidate and min_dist > 0.0 and (level - bid) < min_dist:
                    is_candidate = False

            if is_candidate:
                if blocked_k:
                    level_k = round((level - anchor) / step)
                    if level_k in blocked_k:
                        k += k_delta
                        continue
                targets.append(level)

            k += k_delta

        return targets

    # ✨ 关键修改点 4：完全移除原 _should_skip_level 方法（已内联合并）


# ── Risk Manager ──────────────────────────────────────────────────────────────

class RangeAction(Enum):
    OK = "ok"
    FREEZE = "freeze"
    STOP = "stop"

@dataclass
class SpreadCheck:
    triggered: bool
    spread: float
    pause_until: float

class RiskManager:
    EPSILON = 1e-9

    def check_spread(self, *, bid: float, ask: float, max_spread_points: Optional[float], point: float, extreme_cooldown: float, now: float) -> SpreadCheck:
        if max_spread_points is None or max_spread_points <= 0 or point <= 0:
            return SpreadCheck(False, 0.0, 0.0)
        spread = ask - bid
        # [修复 L-04] 负点差（ask < bid）属于 tick 数据异常，与真实点差过大性质不同。
        # 异常 tick 不触发极端冷却，直接放行让上层的 bid<=0 守卫或市场开盘检查处理。
        if spread < 0:
            return SpreadCheck(False, spread, 0.0)
        if spread > max_spread_points * point:
            return SpreadCheck(True, spread, now + extreme_cooldown)
        return SpreadCheck(False, spread, 0.0)

    def check_range(self, *, mid_price: float, min_price: float, max_price: float, out_of_range_action: str) -> RangeAction:
        if min_price >= max_price:
            return RangeAction.FREEZE
        if mid_price < min_price or mid_price > max_price:
            return RangeAction.STOP if str(out_of_range_action).lower().strip() == "stop" else RangeAction.FREEZE
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
        if lot <= 0:
            return False

        eps = self.EPSILON
        
        # ✨ 关键修改点 5：前置并统一定义状态与中间变量，消除各分支下的冗余计算
        is_buy = side == "buy"
        total_long = long_vol + pending_buy_vol
        total_short = short_vol + pending_sell_vol
        
        effective_net_vol = net_vol if net_vol is not None else (total_long - total_short)
        new_net = effective_net_vol + lot if is_buy else effective_net_vol - lot

        # 单边持仓量限制检查
        if is_buy:
            if max_long_vol is not None and (total_long + lot) > max_long_vol + eps:
                return False
            if max_long_pos is not None and (long_pos_count + 1) > max_long_pos:
                return False
        else:
            if max_short_vol is not None and (total_short + lot) > max_short_vol + eps:
                return False
            if max_short_pos is not None and (short_pos_count + 1) > max_short_pos:
                return False

        if max_net_vol is None:
            return True

        cap = float(max_net_vol)

        # ✨ 关键修改点 6：各模式校验直接复用已算好的 new_net、total_long 等变量，逻辑更平铺
        if mode == "neutral":
            if abs(effective_net_vol) > cap + eps:
                return abs(new_net) < abs(effective_net_vol) - eps
            return abs(new_net) <= cap + eps

        if mode == "long":
            if is_buy:
                return (total_long + lot) <= cap + eps
            return hedge_enabled and new_net >= -eps

        if mode == "short":
            if not is_buy:
                return (total_short + lot) <= cap + eps
            return hedge_enabled and new_net <= eps

        return True
