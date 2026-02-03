from .params import HedgeParams
from .state import RuntimeState
from .mt5_gateway import MT5Gateway
import time
import MetaTrader5 as mt5
from typing import Optional

class HedgeEngine:
    def __init__(self, params: HedgeParams, gateway: MT5Gateway, normalize_vol_fn, normalize_price_fn, magic, symbol):
        self.params = params
        self.gateway = gateway
        self.normalize_vol = normalize_vol_fn
        self.normalize_price = normalize_price_fn
        self.magic = magic
        self.symbol = symbol

    def _get_m1_rates_cached(self, datafeed, n: int = 450, cache_sec: int = 10):
        if datafeed is not None:
            return datafeed.get_rates(
                self.symbol,
                mt5.TIMEFRAME_M1,
                n,
                cache_seconds=cache_sec,
                min_ratio=0.7,
            )

        rates = self.gateway.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, n)
        
        if rates is None or len(rates) < int(n * 0.7):
            return None

        return rates

    def _quantile(self, arr, q: float):
        xs = sorted(arr)
        if not xs: return None
        if q <= 0: return xs[0]
        if q >= 1: return xs[-1]
        pos = (len(xs) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(xs) - 1)
        frac = pos - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    def _volatility_gate(self, rates):
        lb = self.params.vol_lookback
        win = self.params.vol_window
        q = self.params.vol_quantile
        r = rates[-lb:] if len(rates) >= lb else rates
        ranges = [float(x["high"] - x["low"]) for x in r]
        if len(ranges) < win + 10:
            return False, None, None
        cur = sum(ranges[-win:]) / win
        thr = self._quantile(ranges, q)
        return (thr is not None and cur >= thr), cur, thr

    def _volume_gate(self, rates):
        base = self.params.vol_base
        win = self.params.vol_window
        mult = self.params.vol_mult
        v = [float(x["tick_volume"]) for x in rates]
        if len(v) < base + win + 10:
            return False, None, None
        cur = sum(v[-win:]) / win
        basev = sum(v[-(base + win):-win]) / base
        if basev <= 0:
            return False, cur, basev
        return cur >= mult * basev, cur, basev

    def check_and_execute_hedge(self, 
                                net_vol: float, 
                                short_vol: float, 
                                current_gross: float, 
                                max_gross: Optional[float],
                                cap: float,
                                step: float, 
                                mid_price: float, 
                                tick, 
                                state: RuntimeState,
                                datafeed):
        
        action_results = []
        hedge_target = cap * self.params.fraction
        tranche = hedge_target / max(1, self.params.tranches)
        now = time.time()
        
        # (C) Entry logic
        if net_vol >= cap and short_vol < hedge_target:
             rates = self._get_m1_rates_cached(datafeed, n=450, cache_sec=10)
             vol_ok, vol_cur, vol_thr = (False, None, None)
             volm_ok, v_cur, v_base = (False, None, None)
             
             if rates is not None:
                vol_ok, vol_cur, vol_thr = self._volatility_gate(rates)
                volm_ok, v_cur, v_base = self._volume_gate(rates)

             gate_ok = (vol_ok and volm_ok)
             
             if gate_ok and (now - state.last_hedge_time >= self.params.cooldown):
                 ok_move = (
                    state.last_hedge_entry_price is None or
                    mid_price <= state.last_hedge_entry_price - self.params.entry_steps * step
                 )
                 
                 if ok_move:
                     vol_to_add = min(tranche, hedge_target - short_vol)
                     
                     if max_gross is None or (current_gross + vol_to_add <= max_gross):
                        vol_norm = self.normalize_vol(vol_to_add)
                        price_norm = self.normalize_price(tick.bid)
                        
                        req = {
                            "action": mt5.TRADE_ACTION_DEAL,
                            "symbol": self.symbol,
                            "volume": vol_norm,
                            "type": mt5.ORDER_TYPE_SELL,
                            "price": price_norm,
                            "deviation": 20,
                            "magic": self.magic,
                            "comment": "HEDGE_SELL",
                            "type_time": mt5.ORDER_TIME_GTC,
                        }
                        
                        action_results.append({
                            "type": "OPEN",
                            "req": req,
                            "log": (f"Add={vol_to_add:6.2f} Short={short_vol:6.2f}/{hedge_target:6.2f} "
                                    f"Net={net_vol:6.2f}/{cap:6.2f} | Volatility={vol_cur:6.3f}>={vol_thr:6.3f} "
                                    f"Volume={v_cur:6.1f}>={self.params.vol_mult}x{v_base:6.1f}"),
                            "new_entry_price": mid_price
                        })

        # (D) Exit logic
        safe_net = cap * (1.0 - self.params.fraction)
        rebound = False
        if state.last_hedge_entry_price is not None:
            rebound = mid_price >= state.last_hedge_entry_price + self.params.exit_steps * step
        
        if short_vol > 0 and (now - state.last_hedge_time >= self.params.cooldown):
            if rebound or net_vol <= safe_net:
                action_results.append({"type": "CHECK_EXIT", "tranche": tranche, "rebound": rebound, "safe_net": safe_net})

        return action_results
