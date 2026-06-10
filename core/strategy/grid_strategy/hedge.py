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
        if arr is None or len(arr) == 0:
            return None
        return float(np.quantile(arr, np.clip(q, 0.0, 1.0)))

    def _log_hedge_gate_warning(self, key: str, message: str, *, min_interval: float = 60.0) -> None:
        now = time.time()
        attr = f"_last_{key}_warn_time"
        last = float(getattr(self, attr, 0.0) or 0.0)
        if now - last >= min_interval:
            setattr(self, attr, now)
            Logger.log(self.symbol, "WARN", message)

    def _volatility_gate(self, rates):
        lb, win, q = self.hedge_vol_lookback, self.hedge_vol_window, self.hedge_vol_quantile
        r = rates[-lb:] if len(rates) >= lb else rates
        ranges = r["high"] - r["low"]

        if len(ranges) < max(3, int(win * 0.5)):
            self._log_hedge_gate_warning(
                "hedge_vol_gate",
                f"对冲波动门控数据不足，已阻断对冲: bars={len(ranges)} need>={max(3, int(win * 0.5))}",
            )
            return False, None, None

        effective_win = min(win, len(ranges))
        if len(ranges) < win + 10:
            self._log_hedge_gate_warning(
                "hedge_vol_gate_degraded",
                f"对冲波动门控数据偏少，使用短窗口退化计算: bars={len(ranges)} window={effective_win}",
            )

        cur = np.mean(ranges[-effective_win:])
        thr = self._quantile(ranges, q)
        return (thr is not None and cur >= thr), float(cur), thr

    def _volume_gate(self, rates):
        base, win, mult = self.hedge_vol_base, self.hedge_vol_window, self.hedge_vol_mult
        v = rates["tick_volume"]
        min_needed = max(6, int(win * 2))
        if len(v) < min_needed:
            self._log_hedge_gate_warning(
                "hedge_volume_gate",
                f"对冲成交量门控数据不足，已阻断对冲: bars={len(v)} need>={min_needed}",
            )
            return False, None, None

        effective_win = min(win, max(1, len(v) // 3))
        base_slice = v[: -effective_win]
        if len(v) < base + win + 10:
            self._log_hedge_gate_warning(
                "hedge_volume_gate_degraded",
                f"对冲成交量门控数据偏少，使用可用历史退化计算: bars={len(v)} window={effective_win}",
            )
        if len(base_slice) == 0:
            return False, None, None

        cur = np.mean(v[-effective_win:])
        basev = np.mean(base_slice[-base:])

        if basev <= 0:
            return False, float(cur), float(basev)
        return cur >= mult * basev, float(cur), float(basev)

    def _normalize_partial_volume(self, requested: float, *, cap: float | None = None) -> float:
        """Normalize partial volume without exceeding the requested cap."""
        try:
            vol = float(requested)
        except Exception:
            return 0.0
        if vol <= 0:
            return 0.0

        max_allowed = float(cap) if cap is not None else vol
        max_allowed = max(0.0, max_allowed)
        if max_allowed <= 0:
            return 0.0

        vol = min(vol, max_allowed)
        step = float(getattr(self, "vol_step", 0.0) or 0.0)
        if step > 0:
            steps = int((vol / step) + 1e-12)
            vol = steps * step

        v_min = float(getattr(self, "vol_min", 0.0) or 0.0)
        if vol < v_min:
            return 0.0

        return min(self._normalize_volume(vol), max_allowed)

    @staticmethod
    def _select_hedge_exit_position(short_positions, ask_price: float):
        if not short_positions:
            return None
        # Prioritize closing the most profitable hedge leg first.
        return max(short_positions, key=lambda p: ((float(p.price_open) - float(ask_price)), -int(p.ticket)))

    def _open_hedge_sell(self, vol, tick=None):
        if tick is None and (tick := self._get_tick()) is None:
            return None
        vol = float(vol or 0.0)
        if vol <= 0:
            return None

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": vol,
            "type": mt5.ORDER_TYPE_SELL,
            "price": self._normalize_price(tick.bid),
            "deviation": 20,
            "magic": self.magic,
            "comment": "HEDGE_SELL",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        return self._send_with_fillings(req)

    def _close_sell_position(self, pos_ticket, vol, tick=None):
        if tick is None and (tick := self._get_tick()) is None:
            return None
        vol = float(vol or 0.0)
        if vol <= 0:
            return None

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": int(pos_ticket),
            "volume": vol,
            "type": mt5.ORDER_TYPE_BUY,
            "price": self._normalize_price(tick.ask),
            "deviation": 20,
            "magic": self.magic,
            "comment": "HEDGE_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        return self._send_with_fillings(req)

    def _move_sell_sl_to_breakeven(self, pos, tick=None):
        if tick is None and (tick := self._get_tick()) is None:
            return None

        if tick.ask >= pos.price_open:
            return None

        # Short BE should protect below entry, not above entry.
        sl = self._normalize_price(pos.price_open - self.be_buffer_points * self.point)
        if pos.sl and float(pos.sl) <= sl:
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

    def _run_hedge_manager(self, my_positions, tick, *, predicted_net_vol: float | None = None):
        long_pos, short_pos = [], []
        long_vol = short_vol = 0.0

        for p in my_positions:
            if p.type == mt5.POSITION_TYPE_BUY:
                long_pos.append(p)
                long_vol += p.volume
            elif p.type == mt5.POSITION_TYPE_SELL:
                short_pos.append(p)
                short_vol += p.volume

        net_vol = long_vol - short_vol
        net_for_trigger = float(predicted_net_vol) if predicted_net_vol is not None else net_vol
        cap = float(self.max_net_vol)
        hedge_target = cap * self.hedge_fraction

        gross = long_vol + short_vol
        at_gross_cap = self.max_gross_vol is not None and gross >= self.max_gross_vol

        now = time.time()
        mid = (tick.bid + tick.ask) * 0.5
        step = self.step

        be_trigger = self.be_trigger_steps * step
        for pos in short_pos:
            if (pos.price_open - tick.ask) >= be_trigger:
                self._move_sell_sl_to_breakeven(pos, tick=tick)

        rates = self._get_m1_rates_cached(n=450, cache_sec=10)
        gate_ok = False
        vol_cur = vol_thr = v_cur = v_base = None

        if rates is not None:
            vol_ok, vol_cur, vol_thr = self._volatility_gate(rates)
            volm_ok, v_cur, v_base = self._volume_gate(rates)
            gate_ok = vol_ok and volm_ok
        else:
            self._log_hedge_gate_warning(
                "hedge_rates_missing",
                "对冲门控无法获取 M1 行情数据，已阻断对冲",
            )

        tranche = hedge_target / max(1, self.hedge_tranches)
        if tranche <= 0:
            return

        if (not at_gross_cap) and net_for_trigger >= cap and short_vol < hedge_target:
            if gate_ok and (now - self._last_hedge_time >= self.hedge_cooldown):
                if (self._last_hedge_entry_price is None or
                    mid <= self._last_hedge_entry_price - self.hedge_entry_steps * step):

                    remaining_hedge = max(0.0, hedge_target - short_vol)
                    vol_to_add = self._normalize_partial_volume(
                        min(tranche, remaining_hedge),
                        cap=remaining_hedge,
                    )
                    if vol_to_add > 0 and (self.max_gross_vol is None or (gross + vol_to_add <= self.max_gross_vol)):
                        res = self._open_hedge_sell(vol_to_add, tick=tick)
                        if res and getattr(res, "queued", False):
                            # Queue mode: mark cooldown immediately to avoid duplicate hedges
                            # before asynchronous execution updates account state.
                            self._last_hedge_time = now
                            self._last_hedge_entry_price = mid
                            Logger.log(
                                self.symbol,
                                "HEDGE_ADD",
                                f"Magic={self.magic:04d} | Add={vol_to_add:6.2f} queued | NetPred={net_for_trigger:6.2f}/{cap:6.2f}",
                            )
                        if (
                            res
                            and (not getattr(res, "queued", False))
                            and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
                        ):
                            self._last_hedge_time = now
                            self._last_hedge_entry_price = mid
                            Logger.log(
                                self.symbol,
                                "HEDGE_ADD",
                                f"Magic={self.magic:04d} | Add={vol_to_add:6.2f} Short={short_vol:6.2f}/{hedge_target:6.2f} "
                                f"Net={net_vol:6.2f} NetPred={net_for_trigger:6.2f}/{cap:6.2f} | Volatility={(0.0 if vol_cur is None else vol_cur):6.3f}>={(0.0 if vol_thr is None else vol_thr):6.3f} "
                                f"Volume={(0.0 if v_cur is None else v_cur):6.1f}>={self.hedge_vol_mult}x{(0.0 if v_base is None else v_base):6.1f}",
                            )

        safe_net = cap * (1.0 - self.hedge_fraction)
        rebound = (
            self._last_hedge_entry_price is not None
            and mid >= self._last_hedge_entry_price + self.hedge_exit_steps * step
        )

        if short_vol > 0 and (now - self._last_hedge_time >= self.hedge_cooldown):
            if rebound or net_for_trigger <= safe_net:
                target_pos = self._select_hedge_exit_position(short_pos, tick.ask)
                if target_pos is None:
                    return
                vol_to_close = self._normalize_partial_volume(
                    min(tranche, float(target_pos.volume)),
                    cap=float(target_pos.volume),
                )
                if vol_to_close <= 0:
                    return

                res = self._close_sell_position(target_pos.ticket, vol_to_close, tick=tick)
                if res and getattr(res, "queued", False):
                    # Queue mode: throttle repeated exit attempts until queued request is flushed.
                    self._last_hedge_time = now
                    Logger.log(
                        self.symbol,
                        "HEDGE_EXIT",
                        f"Magic={self.magic:04d} | Close={vol_to_close:6.2f} queued | NetPred={net_for_trigger:6.2f}",
                    )
                if (
                    res
                    and (not getattr(res, "queued", False))
                    and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
                ):
                    self._last_hedge_time = now
                    Logger.log(
                        self.symbol,
                        "HEDGE_EXIT",
                        f"Magic={self.magic:04d} | Close={vol_to_close:6.2f} Net={net_vol:6.2f} NetPred={net_for_trigger:6.2f} "
                        f"SafeLevel={safe_net:6.2f} | Rebound={rebound}",
                    )
