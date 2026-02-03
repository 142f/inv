from typing import List, Dict, Set, Optional

class OrderIndex:
    def __init__(self):
        self.by_price_buy: Dict[float, List] = {}
        self.by_price_sell: Dict[float, List] = {}
        self.prices_buy: Set[float] = set()
        self.prices_sell: Set[float] = set()
        self.tickets: Set[int] = set()
        self.all_orders = []

    def rebuild(self, orders: List, normalize_price_fn):
        self.by_price_buy.clear()
        self.by_price_sell.clear()
        self.prices_buy.clear()
        self.prices_sell.clear()
        self.tickets.clear()
        self.all_orders = orders

        for o in orders:
            self.tickets.add(o.ticket)
            price_norm = normalize_price_fn(o.price_open)
            
            # Assuming type 0 is BUY, 1 is SELL, 2 is BUY_LIMIT, etc.
            # In GridStrategy, pending orders are usually LIMIT
            # MT5: ORDER_TYPE_BUY_LIMIT=2, ORDER_TYPE_SELL_LIMIT=3
            # But the original code handles existing orders. 
            # We treat Buy Limit as potential Buy.
            
            # Simple heuristic matching original logic (orders_get usually returns pending)
            is_buy = (o.type == 0 or o.type == 2 or o.type == 4) # BUY, BUY_LIMIT, BUY_STOP
            is_sell = (o.type == 1 or o.type == 3 or o.type == 5)
            
            if is_buy:
                if price_norm not in self.by_price_buy:
                    self.by_price_buy[price_norm] = []
                self.by_price_buy[price_norm].append(o)
                self.prices_buy.add(price_norm)
            elif is_sell:
                if price_norm not in self.by_price_sell:
                    self.by_price_sell[price_norm] = []
                self.by_price_sell[price_norm].append(o)
                self.prices_sell.add(price_norm)

    def get_orders_at(self, price: float, side: str) -> List:
        if side == 'buy':
            return self.by_price_buy.get(price, [])
        return self.by_price_sell.get(price, [])

    def has_price(self, side: str, price: float) -> bool:
        if side == 'buy':
            return price in self.prices_buy
        return price in self.prices_sell
