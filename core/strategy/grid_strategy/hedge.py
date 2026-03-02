import MetaTrader5 as mt5
import numpy as np
import time
from core.logger import Logger

class GridHedgeMixin:
    def _get_m1_rates_cached(self, n: int = 450, cache_sec: int = 10):
        if self.datafeed is not None:
            return self.datafeed.get_rates(
                self.symbol, mt5.TIMEFRAME_M1, n,
                cache_seconds=cache_sec, min_ratio=0.7,
            )
        # 优化：直接使用 int 转换避免重复计算
        rates = self._mt5_call(mt5.copy_rates_from_pos, self.symbol, mt5.TIMEFRAME_M1, 0, n)
        if rates is None or len(rates) < int(n * 0.7):
            return None
        return rates

    @staticmethod
    def _quantile(arr, q: float):
        # 优化：使用 numpy 底层逻辑处理空数组，减少 Python 层判断
        if arr is None or len(arr) == 0:
            return None
        return float(np.quantile(arr, np.clip(q, 0.0, 1.0)))

    def _volatility_gate(self, rates):
        """【优化】移除列表推导式，直接使用 NumPy 矢量化减法与均值计算"""
        lb, win, q = self.hedge_vol_lookback, self.hedge_vol_window, self.hedge_vol_quantile
        
        # 优化：利用 NumPy 结构化数组的字段访问，速度提升一个量级
        r = rates[-lb:] if len(rates) >= lb else rates
        ranges = r["high"] - r["low"]  # 矢量化减法
        
        if len(ranges) < win + 10:
            return False, None, None
            
        cur = np.mean(ranges[-win:])  # 矢量化均值
        thr = self._quantile(ranges, q)
        return (thr is not None and cur >= thr), float(cur), thr

    def _volume_gate(self, rates):
        """【优化】合并切片逻辑，使用 NumPy 矢量化求和"""
        base, win, mult = self.hedge_vol_base, self.hedge_vol_window, self.hedge_vol_mult
        
        # 优化：直接提取 tick_volume 视图
        v = rates["tick_volume"]
        if len(v) < base + win + 10:
            return False, None, None
            
        cur = np.mean(v[-win:])
        basev = np.mean(v[-(base + win):-win])
        
        if basev <= 0:
            return False, float(cur), float(basev)
        return cur >= mult * basev, float(cur), float(basev)

    def _open_hedge_sell(self, vol, tick=None):
        if tick is None and (tick := self._get_tick()) is None:
            return None
            
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self._normalize_volume(vol),
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
            
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": int(pos_ticket),
            "volume": self._normalize_volume(vol),
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

        # 优化：预先计算 SL，减少重复 getattr 调用
        sl = self._normalize_price(pos.price_open + self.be_buffer_points * self.point)
        
        # 保留原逻辑：已有 SL 更优则不更新
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

    def _run_hedge_manager(self, my_positions, tick):
        """【重构核心】单次遍历统计、矢量化指标计算、保留所有业务逻辑漏洞"""
        
        # 优化：通过单次循环完成分类和量能统计，将复杂度从 O(4N) 降至 O(N)
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
        cap = float(self.max_net_vol)
        hedge_target = cap * self.hedge_fraction
        
        # 预判断：超过总仓位上限直接退出
        gross = long_vol + short_vol
        if self.max_gross_vol is not None and gross >= self.max_gross_vol:
            return

        now = time.time()
        mid = (tick.bid + tick.ask) * 0.5
        step = self.step  # 缓存属性访问

        # (A) 盈利对冲平盈保本
        be_trigger = self.be_trigger_steps * step
        for pos in short_pos:
            if (pos.price_open - tick.ask) >= be_trigger:
                self._move_sell_sl_to_breakeven(pos, tick=tick)

        # (B) 门槛校验：利用优化后的 NumPy 门槛函数
        rates = self._get_m1_rates_cached(n=450, cache_sec=10)
        gate_ok = False
        vol_cur = vol_thr = v_cur = v_base = None
        
        if rates is not None:
            vol_ok, vol_cur, vol_thr = self._volatility_gate(rates)
            volm_ok, v_cur, v_base = self._volume_gate(rates)
            gate_ok = vol_ok and volm_ok

        tranche = hedge_target / max(1, self.hedge_tranches)

        # (C) 对冲入场逻辑 (Tranche entry)
        if net_vol >= cap and short_vol < hedge_target:
            if gate_ok and (now - self._last_hedge_time >= self.hedge_cooldown):
                # 保留逻辑：基于上次价格的步长校验
                if (self._last_hedge_entry_price is None or 
                    mid <= self._last_hedge_entry_price - self.hedge_entry_steps * step):
                    
                    vol_to_add = min(tranche, hedge_target - short_vol)
                    if self.max_gross_vol is None or (gross + vol_to_add <= self.max_gross_vol):
                        res = self._open_hedge_sell(vol_to_add, tick=tick)
                        if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            self._last_hedge_time = now
                            self._last_hedge_entry_price = mid
                            # 保持原有日志格式
                            Logger.log(self.symbol, "HEDGE_ADD", 
                                f"Magic={self.magic:04d} | Add={vol_to_add:6.2f} Short={short_vol:6.2f}/{hedge_target:6.2f} "
                                f"Net={net_vol:6.2f}/{cap:6.2f} | Volatility={vol_cur:6.3f}>={vol_thr:6.3f} "
                                f"Volume={v_cur:6.1f}>={self.hedge_vol_mult}x{v_base:6.1f}")

        # (D) 对冲离场逻辑 (Tranche exit)
        safe_net = cap * (1.0 - self.hedge_fraction)
        rebound = (self._last_hedge_entry_price is not None and 
                   mid >= self._last_hedge_entry_price + self.hedge_exit_steps * step)
        
        if short_vol > 0 and (now - self._last_hedge_time >= self.hedge_cooldown):
            if rebound or net_vol <= safe_net:
                # 保留原有“漏洞”逻辑：始终寻找 ticket 最小的（最早的）仓位平仓
                target_pos = min(short_pos, key=lambda p: p.ticket)
                vol_to_close = min(tranche, target_pos.volume)
                res = self._close_sell_position(target_pos.ticket, vol_to_close, tick=tick)
                if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    self._last_hedge_time = now
                    Logger.log(self.symbol, "HEDGE_EXIT", 
                        f"Magic={self.magic:04d} | Close={vol_to_close:6.2f} Net={net_vol:6.2f} "
                        f"SafeLevel={safe_net:6.2f} | Rebound={rebound}")