"""运行时混入组件：MT5基础调用、品种信息及状态/统计管理。"""

from __future__ import annotations

import time
from datetime import datetime

import MetaTrader5 as mt5

from core.logger import Logger

# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------

_MARKET_TICK_MAX_AGE_SECONDS = 600

# [P-06] _order_profit / _order_type 字典 FIFO 容量上限
_MAX_ORDER_HISTORY = 20_000

ALLOWED_FILLING_MODES = (
    mt5.ORDER_FILLING_FOK,
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_RETURN,
)

FILLING_FALLBACK_ORDER = (
    mt5.ORDER_FILLING_RETURN,
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_FOK,
)


def apply_default_filling_mode(request, filling_mode):
    if request is None:
        return None
    if not isinstance(request, dict):
        return request
    action = request.get("action")
    if action not in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING):
        return request
    if "type_filling" in request:
        return request
    if filling_mode in ALLOWED_FILLING_MODES:
        req = dict(request)
        req["type_filling"] = filling_mode
        return req
    return request


def iter_filling_candidates(default_mode):
    seen = set()
    if default_mode in ALLOWED_FILLING_MODES:
        seen.add(default_mode)
        yield default_mode
    for mode in FILLING_FALLBACK_ORDER:
        if mode in seen:
            continue
        seen.add(mode)
        yield mode


class QueuedResult:
    def __init__(self):
        self.retcode = -1
        self.comment = "QUEUED"
        self.order = 0
        self.queued = True


# ---------------------------------------------------------------------------
# GridRuntimeMixin
# ---------------------------------------------------------------------------

class GridRuntimeMixin:
    def _mt5_call(self, func, *args, **kwargs):
        """包装 MT5 API 调用，安全接入共享锁。"""
        # [优化]：展平执行逻辑，剔除 `_execute` 内部函数的动态分配，降低堆内存碎片与函数调用栈深度。
        if self.lock:
            with self.lock:
                if func in (mt5.order_check, mt5.order_send) and args and isinstance(args[0], dict):
                    return func(args[0])
                return func(*args, **kwargs)
        else:
            if func in (mt5.order_check, mt5.order_send) and args and isinstance(args[0], dict):
                return func(args[0])
            return func(*args, **kwargs)

    def _get_tick(self):
        return self._mt5_call(mt5.symbol_info_tick, self.symbol)

    def _is_market_open(self, tick=None):
        if tick is None:
            tick = self._get_tick()
        if not tick:
            return False
        if abs(time.time() - tick.time) > _MARKET_TICK_MAX_AGE_SECONDS:
            return False
        return True

    def _prepare_request(self, request):
        return apply_default_filling_mode(request, self.filling_mode)

    def _append_action_if_queued(self, request):
        if getattr(self, "_action_collector", None) is None:
            return False
        if request is not None:
            self._action_collector.append(request)
        return True

    def _queue_action(self, request):
        request = self._prepare_request(request)
        return self._append_action_if_queued(request)

    def _dispatch_request(self, request):
        request = self._prepare_request(request)
        if self._append_action_if_queued(request):
            return QueuedResult()
        return self._mt5_call(mt5.order_send, request)


# ---------------------------------------------------------------------------
# GridStateStatsMixin
# ---------------------------------------------------------------------------

class GridStateStatsMixin:
    def get_state(self):
        """返回策略内部状态用于持久化同步。"""
        # [修复 L-02]：完整序列化游标时间、Ticket 及订单缓存状态，防止重启丢失
        return {
            'pause_until': getattr(self, 'pause_until', 0.0),
            'enabled': getattr(self, 'enabled', False),
            '_last_atr_value': getattr(self, '_last_atr_value', None),
            '_last_atr_time': getattr(self, '_last_atr_time', 0.0),
            'anchor': getattr(self, 'anchor', None),
            '_last_recenter_time': getattr(self, '_last_recenter_time', 0.0),
            "_last_hedge_time": getattr(self, "_last_hedge_time", 0.0),
            "_last_hedge_entry_price": getattr(self, "_last_hedge_entry_price", None),
            '_stats': getattr(self, '_stats', {}),
            # 补齐丢失的增量游标与状态字典
            '_last_deal_time': getattr(self, '_last_deal_time', 0.0),
            '_last_deal_ticket': getattr(self, '_last_deal_ticket', 0),
            '_order_profit': getattr(self, '_order_profit', {}),
            '_order_type': getattr(self, '_order_type', {}),
        }

    def set_state(self, state):
        """恢复策略内部状态。"""
        if state:
            self.pause_until = state.get('pause_until', getattr(self, 'pause_until', 0.0))
            self.enabled = state.get('enabled', getattr(self, 'enabled', False))
            self._last_atr_value = state.get('_last_atr_value', getattr(self, '_last_atr_value', None))
            self._last_atr_time = state.get('_last_atr_time', getattr(self, '_last_atr_time', 0.0))
            self.anchor = state.get('anchor', getattr(self, 'anchor', None))
            self._last_recenter_time = state.get('_last_recenter_time', getattr(self, '_last_recenter_time', 0.0))
            self._last_hedge_time = float(state.get("_last_hedge_time", getattr(self, "_last_hedge_time", 0.0)) or 0.0)
            self._last_hedge_entry_price = state.get("_last_hedge_entry_price", getattr(self, "_last_hedge_entry_price", None))
            
            if '_stats' in state:
                self._stats = state['_stats']
            
            # [修复 L-02]：反序列化增量游标与缓存
            self._last_deal_time = state.get('_last_deal_time', 0.0)
            self._last_deal_ticket = state.get('_last_deal_ticket', 0)
            self._order_profit = self._restore_ticket_key_dict(state.get('_order_profit', {}))
            self._order_type = self._restore_ticket_key_dict(state.get('_order_type', {}))

    @staticmethod
    def _restore_ticket_key_dict(value):
        if not isinstance(value, dict):
            return {}
        restored = {}
        for key, item in value.items():
            try:
                restored[int(key)] = item
            except (TypeError, ValueError):
                restored[key] = item
        return restored

    def _deal_net_profit(self, deal) -> float:
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        return profit + swap + commission

    def _deal_time_value(self, deal) -> float:
        """获取交易流水的时间戳。"""
        # [优化]：MT5 严格保证 deal.time 返回 Unix 时间戳（整数）。
        # 移除高昂的 hasattr 反射与双层 try-except，提升处理大规模历史流水的效率。
        return float(getattr(deal, "time", 0))

    def _adjust_profitable_stats(self, order_type: str, amount_delta: float, count_delta: int = 0) -> None:
        if order_type == "long":
            self._stats['long_profitable_amount'] += amount_delta
            self._stats['long_profitable_count'] += count_delta
        elif order_type == "short":
            self._stats['short_profitable_amount'] += amount_delta
            self._stats['short_profitable_count'] += count_delta

    def _apply_deal_to_stats(self, deal) -> None:
        order_ticket = getattr(deal, "order", None)
        if order_ticket is None:
            return

        delta = self._deal_net_profit(deal)
        prev_total = float(self._order_profit.get(order_ticket, 0.0) or 0.0)

        order_type = self._order_type.get(order_ticket)
        type_was_known = order_type is not None
        if order_type is None:
            if deal.type == mt5.DEAL_TYPE_BUY:
                order_type = "long"
            elif deal.type == mt5.DEAL_TYPE_SELL:
                order_type = "short"
            if order_type:
                self._order_type[order_ticket] = order_type

        was_positive = prev_total > 0.0
        if not type_was_known:
            was_positive = False

        new_total = prev_total + delta
        self._order_profit[order_ticket] = new_total
        is_positive = new_total > 0.0

        if not order_type:
            return

        if was_positive:
            if is_positive:
                self._adjust_profitable_stats(order_type, delta, 0)
            else:
                self._adjust_profitable_stats(order_type, -prev_total, -1)
        else:
            if is_positive:
                self._adjust_profitable_stats(order_type, new_total, 1)

        # [P-06] 防止长期运行积累数十万条目：超出上限时 FIFO 驱逐最旧条目
        # Python 3.7+ dict 保持插入顺序，iter() 返回最旧键
        if len(self._order_profit) > _MAX_ORDER_HISTORY:
            try:
                oldest_key = next(iter(self._order_profit))
                del self._order_profit[oldest_key]
                self._order_type.pop(oldest_key, None)
            except StopIteration:
                pass

    def _update_stats(self):
        """增量更新新成交流水的统计信息。"""
        now = time.time()
        if now - self._stats.get('last_stats_update_time', 0) < 300:
            return

        try:
            start_time = getattr(self, '_last_deal_time', 0.0)
            if not start_time:
                start_time = self._stats.get('last_reset_time', 0.0)
                
            # [修复 L-01]：严格遵守 MT5 的日期范围查询签名，通过 datetime 对象传入时间区间
            date_from = datetime.fromtimestamp(start_time)
            date_to = datetime.now()
            deals = self._mt5_call(mt5.history_deals_get, date_from, date_to, group="*")

            if deals:
                max_time = float(self._last_deal_time or 0.0)
                max_ticket = int(self._last_deal_ticket or 0)
                
                for deal in deals:
                    # 将 magic 和 symbol 过滤转移到 Python 层面进行
                    if deal.magic != self.magic or deal.symbol != self.symbol:
                        continue

                    deal_time = self._deal_time_value(deal)
                    deal_ticket = int(getattr(deal, "ticket", 0) or 0)
                    
                    if (deal_time < self._last_deal_time) or (
                        deal_time == self._last_deal_time and deal_ticket <= self._last_deal_ticket
                    ):
                        continue

                    self._apply_deal_to_stats(deal)

                    if (deal_time > max_time) or (deal_time == max_time and deal_ticket > max_ticket):
                        max_time = deal_time
                        max_ticket = deal_ticket

                self._last_deal_time = max_time
                self._last_deal_ticket = max_ticket

            self._stats['last_stats_update_time'] = now

        except Exception as e:
            Logger.log(getattr(self, "symbol", "UNKNOWN"), "EXCEPTION", f"_update_stats 底层发生异常: {str(e)}")


# ---------------------------------------------------------------------------
# GridSymbolMixin
# ---------------------------------------------------------------------------

class GridSymbolMixin:
    def set_symbol(self, new_symbol: str, *, reset_runtime_state: bool = True):
        """切换交易品种并刷新品种信息缓存。"""
        if not new_symbol or new_symbol == getattr(self, "symbol", None):
            return

        self.symbol = new_symbol
        self._cache_symbol_info()

        if reset_runtime_state:
            self.anchor = None
            self._last_recenter_time = 0.0
            self.pause_until = 0.0

            self._last_atr_value = None
            self._last_atr_time = 0.0
            self._last_adapt_bar_time = 0.0

            self._last_hedge_time = 0.0
            self._last_hedge_entry_price = None

    @staticmethod
    def _precision_from_step(step: float) -> int:
        if step <= 0:
            return 2
        text = f"{step:.10f}".rstrip("0").rstrip(".")
        if "." in text:
            return len(text.split(".")[1])
        return 0

    def _cache_symbol_info(self):
        info = self._mt5_call(mt5.symbol_info, getattr(self, "symbol", ""))

        if info:
            self.digits = info.digits
            self.point = info.point
            self.stop_level = info.trade_stops_level * info.point
            self.vol_min = info.volume_min
            self.vol_max = info.volume_max
            self.vol_step = info.volume_step
            self.vol_precision = self._precision_from_step(self.vol_step)
            self.filling_mode = getattr(info, "filling_mode", None)
            self.initialized = True
        else:
            self.digits = 2
            self.point = 0.01
            self.stop_level = 0
            self.vol_min = 0.01
            self.vol_max = 100
            self.vol_step = 0.01
            self.vol_precision = 2
            self.filling_mode = None
            self.initialized = False
            Logger.log(getattr(self, "symbol", "UNKNOWN"), "WARN", "获取品种信息失败，已回退至默认设置。")

    def _normalize_price(self, price):
        return float(round(price, getattr(self, "digits", 2)))

    def _normalize_volume(self, vol):
        if getattr(self, "vol_step", 0) > 0:
            # Floor to broker step to avoid accidental volume inflation by rounding up.
            steps = int((float(vol) / self.vol_step) + 1e-12)
            vol = steps * self.vol_step
            
        # [优化]：移除多余的 getattr 查询。进入此处时 self.vol_precision 已确保被初始化。
        precision = self.vol_precision if getattr(self, "initialized", False) else 2
        
        # 兜底确保 vol_min / vol_max 不抛出属性错误
        v_min = getattr(self, "vol_min", 0.01)
        v_max = getattr(self, "vol_max", 100)
        return float(round(max(v_min, min(v_max, vol)), precision))
