# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import numpy as np
import time
from core.logger import Logger

class GridHedgeMixin:
    def _get_m1_rates_cached(self, n: int = 450, cache_sec: int = 10):
        if self.datafeed is not None:
            return self.datafeed.get_rates(
                self.symbol,
                mt5.TIMEFRAME_M1,
                n,
                cache_seconds=cache_sec,
                min_ratio=0.7,
            )

        rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, mt5.TIMEFRAME_M1, 0, n)

        if rates is None or len(rates) < int(n * 0.7):
            return None

        return rates

    @staticmethod
    def _quantile(arr, q: float):
        if not arr:
            return None
        return float(np.quantile(arr, np.clip(q, 0.0, 1.0)))

    def _volatility_gate(self, rates):
        # range-based vol: high-low
        lb = self.hedge_vol_lookback
        win = self.hedge_vol_window
        q = self.hedge_vol_quantile
        r = rates[-lb:] if len(rates) >= lb else rates
        ranges = [float(x["high"] - x["low"]) for x in r]
        if len(ranges) < win + 10:
            return False, None, None
        cur = sum(ranges[-win:]) / win
        thr = self._quantile(ranges, q)
        return (thr is not None and cur >= thr), cur, thr

    def _volume_gate(self, rates):
        base = self.hedge_vol_base
        win = self.hedge_vol_window
        mult = self.hedge_vol_mult
        v = [float(x["tick_volume"]) for x in rates]
        if len(v) < base + win + 10:
            return False, None, None
        cur = sum(v[-win:]) / win
        basev = sum(v[-(base + win):-win]) / base
        if basev <= 0:
            return False, cur, basev
        return cur >= mult * basev, cur, basev

    def _open_hedge_sell(self, vol, tick=None):
        if tick is None:
            tick = self._get_tick()

        if tick is None:
            return None
        vol = self._normalize_volume(vol)
        price = self._normalize_price(tick.bid)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": vol,
            "type": mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 20,
            "magic": self.magic,
            "comment": "HEDGE_SELL",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        
        return self._send_with_fillings(req)

    def _close_sell_position(self, pos_ticket, vol, tick=None):
        if tick is None:
            tick = self._get_tick()

        if tick is None:
            return None
        vol = self._normalize_volume(vol)
        price = self._normalize_price(tick.ask)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": int(pos_ticket),
            "volume": vol,
            "type": mt5.ORDER_TYPE_BUY,  # BUY 平空
            "price": price,
            "deviation": 20,
            "magic": self.magic,
            "comment": "HEDGE_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        
        return self._send_with_fillings(req)

    def _move_sell_sl_to_breakeven(self, pos, tick=None):
        if tick is None:
            tick = self._get_tick()

        if tick is None:
            return None

        # 空单盈利条件：ask < open
        if tick.ask >= pos.price_open:
            return None

        sl = pos.price_open + self.be_buffer_points * self.point
        sl = self._normalize_price(sl)

        # 若已有SL更紧（更低），不动；若无SL或更松，则更新
        if pos.sl and float(pos.sl) <= float(sl):
            return None

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": int(pos.ticket),
            "sl": sl,
            "tp": pos.tp,
            "magic": self.magic,
            "comment": "HEDGE_BE",
        }
        
        return self._dispatch_request(req)

    def _run_hedge_manager(self, my_positions, tick):
        """Execute the hedge manager logic for long-mode strategies.

        Handles break-even stop adjustment, entry gating, tranche entry/exit.
        Extracted from update() for readability; called only when hedge_enabled,
        mode == "long", and max_net_vol is configured.
        """
        long_pos = [p for p in my_positions if p.type == mt5.POSITION_TYPE_BUY]
        short_pos = [p for p in my_positions if p.type == mt5.POSITION_TYPE_SELL]

        long_vol = sum(p.volume for p in long_pos)
        short_vol = sum(p.volume for p in short_pos)
        net_vol = long_vol - short_vol

        cap = float(self.max_net_vol)
        hedge_target = cap * self.hedge_fraction
        tranche = hedge_target / max(1, self.hedge_tranches)

        gross = long_vol + short_vol
        if self.max_gross_vol is not None and gross >= self.max_gross_vol:
            return  # total position at cap

        now = time.time()
        mid = (tick.bid + tick.ask) / 2.0

        # (A) Break-even stop for profitable hedges
        be_trigger = self.be_trigger_steps * self.step
        for pos in short_pos:
            if (pos.price_open - tick.ask) >= be_trigger:
                self._move_sell_sl_to_breakeven(pos, tick=tick)

        # (B) Entry gates: volatility + volume
        rates = self._get_m1_rates_cached(n=450, cache_sec=10)
        vol_ok, vol_cur, vol_thr = False, None, None
        volm_ok, v_cur, v_base = False, None, None
        if rates is not None:
            vol_ok, vol_cur, vol_thr = self._volatility_gate(rates)
            volm_ok, v_cur, v_base = self._volume_gate(rates)
        gate_ok = vol_ok and volm_ok

        # (C) Tranche entry
        if net_vol >= cap and short_vol < hedge_target:
            if gate_ok and (now - self._last_hedge_time >= self.hedge_cooldown):
                ok_move = (
                    self._last_hedge_entry_price is None
                    or mid <= self._last_hedge_entry_price - self.hedge_entry_steps * self.step
                )
                if ok_move:
                    vol_to_add = min(tranche, hedge_target - short_vol)
                    if self.max_gross_vol is None or (gross + vol_to_add <= self.max_gross_vol):
                        res = self._open_hedge_sell(vol_to_add, tick=tick)
                        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            self._last_hedge_time = now
                            self._last_hedge_entry_price = mid
                            Logger.log(
                                self.symbol,
                                "HEDGE_ADD",
                                f"Magic={self.magic:04d} | Add={vol_to_add:6.2f} Short={short_vol:6.2f}/{hedge_target:6.2f} "
                                f"Net={net_vol:6.2f}/{cap:6.2f} | Volatility={vol_cur:6.3f}>={vol_thr:6.3f} "
                                f"Volume={v_cur:6.1f}>={self.hedge_vol_mult}x{v_base:6.1f}",
                            )

        # (D) Tranche exit on rebound / safe zone
        safe_net = cap * (1.0 - self.hedge_fraction)
        rebound = (
            self._last_hedge_entry_price is not None
            and mid >= self._last_hedge_entry_price + self.hedge_exit_steps * self.step
        )
        if short_vol > 0 and (now - self._last_hedge_time >= self.hedge_cooldown):
            if rebound or net_vol <= safe_net:
                target_pos = min(short_pos, key=lambda p: p.ticket)
                vol_to_close = min(tranche, target_pos.volume)
                res = self._close_sell_position(target_pos.ticket, vol_to_close, tick=tick)
                if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    self._last_hedge_time = now
                    Logger.log(
                        self.symbol,
                        "HEDGE_EXIT",
                        f"Magic={self.magic:04d} | Close={vol_to_close:6.2f} Net={net_vol:6.2f} "
                        f"SafeLevel={safe_net:6.2f} | Rebound={rebound}",
                    )

