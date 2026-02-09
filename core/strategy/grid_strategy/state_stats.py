# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import time
from core.logger import Logger

class GridStateStatsMixin:
    def get_state(self):
        """获取策略内部状态，用于配置同步时保持状态"""
        return {
            'pause_until': self.pause_until,
            'enabled': self.enabled,
            '_last_atr_value': self._last_atr_value,
            '_last_tick_time': self._last_tick_time,
            '_last_atr_time': self._last_atr_time,
            'anchor': self.anchor,
            '_last_recenter_time': self._last_recenter_time,
            "_last_hedge_time": self._last_hedge_time,
            "_last_hedge_entry_price": self._last_hedge_entry_price,
            '_stats': self._stats
        }

    def set_state(self, state):
        """恢复策略内部状态"""
        if state:
            self.pause_until = state.get('pause_until', self.pause_until)
            self.enabled = state.get('enabled', self.enabled)
            self._last_atr_value = state.get('_last_atr_value', self._last_atr_value)
            self._last_tick_time = state.get('_last_tick_time', self._last_tick_time)
            self._last_atr_time = state.get('_last_atr_time', self._last_atr_time)
            self.anchor = state.get('anchor', self.anchor)
            self._last_recenter_time = state.get('_last_recenter_time', self._last_recenter_time)
            self._last_hedge_time = float(state.get("_last_hedge_time", self._last_hedge_time) or 0.0)
            self._last_hedge_entry_price = state.get("_last_hedge_entry_price", self._last_hedge_entry_price)
            # 恢复统计数据
            if '_stats' in state:
                self._stats = state['_stats']

    def _deal_net_profit(self, deal) -> float:
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        return profit + swap + commission

    def _deal_time_value(self, deal) -> float:
        t = getattr(deal, "time", 0)
        if hasattr(t, "timestamp"):
            try:
                return float(t.timestamp())
            except Exception:
                pass
        try:
            return float(t)
        except Exception:
            return 0.0

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

    def _update_stats(self):
        """Update stats incrementally from new deals."""
        now = time.time()
        # Limit update frequency to avoid heavy MT5 calls.
        if now - self._stats['last_stats_update_time'] < 300:  # 5 min
            return

        try:
            start_time = self._last_deal_time if self._last_deal_time else self._stats['last_reset_time']
            deals = self._mt5_call(mt5.history_deals_get, symbol=self.symbol, group="*", start=start_time)

            if deals:
                max_time = float(self._last_deal_time or 0.0)
                max_ticket = int(self._last_deal_ticket or 0)
                for deal in deals:
                    if deal.magic != self.magic:
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
            Logger.log(self.symbol, "EXCEPTION", f"鏇存柊缁熻鏁版嵁寮傚父: {str(e)}")
