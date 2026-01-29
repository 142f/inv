"""Grid target calculator."""

from __future__ import annotations

from typing import Callable, List, Optional, Set, Tuple


class GridCalculator:
    """计算网格目标价位的核心组件。
    
    负责根据锚点(anchor)、步长(step)和窗口大小生成买卖挂单目标价位列表。
    """

    # 安全迭代上限：防止异常参数导致无限循环
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
        """构建买卖目标价位列表。
        
        Args:
            anchor: 网格锚点价格
            step: 网格步长
            min_price: 允许的最低价格
            max_price: 允许的最高价格
            bid: 当前买价
            ask: 当前卖价
            buy_window: 买单窗口大小
            sell_window: 卖单窗口大小
            mode: 交易模式 ("neutral", "long", "short")
            recenter_steps: 重新居中的步数阈值（未使用，保留接口兼容）
            min_dist: 最小挂单距离
            blocked_k: 需要跳过的网格索引集合（已有持仓的层级）
            
        Returns:
            (target_buys, target_sells): 买单和卖单目标价位列表
        """
        target_buys: List[float] = []
        target_sells: List[float] = []

        # 参数校验：步长必须为正数
        if step <= 0:
            return target_buys, target_sells

        # 参数校验：价格范围必须有效
        if max_price <= min_price:
            return target_buys, target_sells

        # 参数校验：bid/ask 必须有效
        if bid <= 0 or ask <= 0 or ask < bid:
            return target_buys, target_sells

        blocked_k = blocked_k or set()
        min_dist = max(0.0, float(min_dist)) if min_dist is not None else 0.0

        def _calc_k(price: float) -> int:
            """计算价格对应的网格索引 k，使用四舍五入避免浮点误差。"""
            return round((price - anchor) / step)

        def _should_skip(level: float, side: str) -> bool:
            """检查某个价位是否应该跳过。"""
            # 最小距离检查
            if min_dist > 0.0:
                if side == "buy" and (ask - level) < min_dist:
                    return True
                if side == "sell" and (level - bid) < min_dist:
                    return True
            # 已有持仓检查
            if blocked_k:
                k = _calc_k(level)
                if k in blocked_k:
                    return True
            return False

        # --- 买单生成（价格低于 Ask）---
        if mode in ("neutral", "long") and buy_window > 0:
            # 从当前 Ask 价附近开始向下搜索
            # 使用 floor 确保起始 k 对应的价格 <= ask
            start_k = int((ask - anchor) / step)
            # 向上检查几个位置以处理浮点精度问题
            start_k += 3
            
            count = 0
            k = start_k
            iterations = 0
            
            while count < buy_window and iterations < self._MAX_ITERATIONS:
                iterations += 1
                level = self._normalize_price(anchor + k * step)
                
                # 终止条件：价格已低于最小价格
                if level < min_price - step:
                    break
                
                # 价格条件检查：买单必须 < ask 且在有效范围内
                if level < ask and min_price <= level <= max_price:
                    if not _should_skip(level, "buy"):
                        target_buys.append(level)
                        count += 1
                
                k -= 1

        # --- 卖单生成（价格高于 Bid）---
        if mode in ("neutral", "short") and sell_window > 0:
            # 从当前 Bid 价附近开始向上搜索
            start_k = int((bid - anchor) / step)
            # 向下检查几个位置以处理浮点精度问题
            start_k -= 3
            
            count = 0
            k = start_k
            iterations = 0
            
            while count < sell_window and iterations < self._MAX_ITERATIONS:
                iterations += 1
                level = self._normalize_price(anchor + k * step)
                
                # 终止条件：价格已高于最大价格
                if level > max_price + step:
                    break
                
                # 价格条件检查：卖单必须 > bid 且在有效范围内
                if level > bid and min_price <= level <= max_price:
                    if not _should_skip(level, "sell"):
                        target_sells.append(level)
                        count += 1
                
                k += 1
            
            # 卖单按价格降序排列（最高价在前）
            target_sells.sort(reverse=True)

        return target_buys, target_sells
