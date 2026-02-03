import time
from .state import StatsState
import MetaTrader5 as mt5

class StatsEngine:
    def __init__(self, magic, symbol, gateway):
        self.magic = magic
        self.symbol = symbol
        self.gateway = gateway
        self.state = StatsState(magic=magic)

    def load_state(self, state_dict):
        if not state_dict:
            return
        for k, v in state_dict.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        # Deep restore for dicts if needed, simple assignment works if state_dict structure matches

    def get_state(self):
        return self.state.__dict__

    def reset(self):
        # Keep start_time
        st = self.state.start_time
        self.state = StatsState(magic=self.magic)
        self.state.start_time = st
        self.state.last_reset_time = time.time()
        self.state.last_deal_time = self.state.last_reset_time

    def update(self):
        now = time.time()
        # Fetch deals since last update
        start = self.state.last_deal_time
        # Safety margin to ensure we don't miss deals, but handle dupes via ticket
        # Or just rely on MT5 history logic
        deals = self.gateway.history_deals_get(
             date_from=max(0, start - 1), 
             date_to=now + 1,
             group=f"*{self.symbol}*"
        )
        
        if deals:
            for d in deals:
                if d.magic != self.magic:
                    continue
                if d.ticket <= self.state.last_deal_ticket:
                    continue
                # Process deal
                self._apply_deal(d)
                if d.time > self.state.last_deal_time:
                    self.state.last_deal_time = float(d.time)
                if d.ticket > self.state.last_deal_ticket:
                    self.state.last_deal_ticket = d.ticket
        
        self.state.last_stats_update_time = now

    def _deal_net_profit(self, deal) -> float:
        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        swap = float(getattr(deal, "swap", 0.0) or 0.0)
        commission = float(getattr(deal, "commission", 0.0) or 0.0)
        return profit + swap + commission

    def _apply_deal(self, deal):
        order_ticket = getattr(deal, "order", None)
        if order_ticket is None:
            return

        delta = self._deal_net_profit(deal)
        prev_total = float(self.state.order_profit.get(order_ticket, 0.0) or 0.0)

        # Infer type if not known
        order_type = self.state.order_type.get(order_ticket)
        type_was_known = order_type is not None
        if order_type is None:
            if deal.type == mt5.DEAL_TYPE_BUY:
                order_type = "ordered_buy" # Deal type buy means we BOUGHT (entry long or exit short)
                # Wait, mapping logic from original file:
                # if deal.type == mt5.DEAL_TYPE_BUY: order_type = "long"
                # This logic in original file was a bit ambiguous, assuming it catches the direction
                # Let's stick to original logic:
                order_type = "long"
            elif deal.type == mt5.DEAL_TYPE_SELL:
                order_type = "short"
            
            if order_type:
                self.state.order_type[order_ticket] = order_type

        was_positive = prev_total > 0.0
        if not type_was_known:
            was_positive = False  # New order tracking

        new_total = prev_total + delta
        self.state.order_profit[order_ticket] = new_total
        is_positive = new_total > 0.0

        # Update counters if profitability flipped
        # Complex logic from original... simplifies to:
        # If it flips from negative/zero to positive, key counts++
        # If it flips positive to negative, key counts--
        
        # Original logic:
        # if not was_positive and is_positive: add(type, +new_total, +1) -> Incorrect interp?
        # Let's look at original strategy_lib.py _apply_deal_to_stats logic:
        # if not was_positive and is_positive: count +1, amount += new_total
        # elif was_positive and not is_positive: count -1, amount -= prev_total
        # elif was_positive and is_positive: amount += delta
        
        if order_type:
            if not was_positive and is_positive:
                self._adjust(order_type, new_total, 1)
            elif was_positive and not is_positive:
                self._adjust(order_type, -prev_total, -1)
            elif was_positive and is_positive:
                self._adjust(order_type, delta, 0)

    def _adjust(self, order_type, amount, count):
        if order_type == "long":
            self.state.long_profitable_amount += amount
            self.state.long_profitable_count += count
        elif order_type == "short":
            self.state.short_profitable_amount += amount
            self.state.short_profitable_count += count
