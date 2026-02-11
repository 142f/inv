from typing import Any, Optional
import MetaTrader5 as mt5

class MT5Gateway:
    def __init__(self, lock: Optional[Any] = None):
        self.lock = lock

    def _call(self, func, *args, **kwargs):
        if self.lock:
            with self.lock:
                # 对于 mt5.order_check 和 mt5.order_send，需要直接传递字典参数
                if func in (mt5.order_check, mt5.order_send) and args and isinstance(args[0], dict):
                    return func(args[0])
                return func(*args, **kwargs)
        # 对于 mt5.order_check 和 mt5.order_send，需要直接传递字典参数
        if func in (mt5.order_check, mt5.order_send) and args and isinstance(args[0], dict):
            return func(args[0])
        return func(*args, **kwargs)

    def orders_get(self, **kwargs):
        return self._call(mt5.orders_get, **kwargs)

    def positions_get(self, **kwargs):
        return self._call(mt5.positions_get, **kwargs)

    def symbol_info_tick(self, symbol):
        return self._call(mt5.symbol_info_tick, symbol)

    def symbol_info(self, symbol):
        return self._call(mt5.symbol_info, symbol)

    def account_info(self):
        return self._call(mt5.account_info)

    def history_deals_get(self, date_from, date_to, **kwargs):
        return self._call(mt5.history_deals_get, date_from, date_to, **kwargs)

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        return self._call(mt5.copy_rates_from_pos, symbol, timeframe, start_pos, count)
    
    def order_send(self, request: dict) -> Any:
        # 使用包装器确保正确传递参数
        from ...mt5_wrapper import order_send
        return order_send(request)
    
    def order_check(self, request: dict) -> Any:
        # 使用包装器确保正确传递参数
        from ...mt5_wrapper import order_check
        return order_check(request)