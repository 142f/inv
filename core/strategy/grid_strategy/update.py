# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import time
from core.logger import Logger
from core.strategy.components import RangeAction


def _fmt_skip(label: str, stats: dict) -> str:
    parts = [f"{k}={v}" for k, v in stats.items() if v]
    if not parts:
        return ""
    return f"{label}({', '.join(parts)})"

# 优化说明：将内嵌函数 _cap_cell 提升至模块级，彻底消除高频调用时的闭包重建开销
def _cap_cell(label: str, value: str, width: int) -> str:
    return Logger._pad_display(f"{label}{value}", width)

class GridUpdateMixin:
    def on_tick(self, ctx, *, action_collector=None):
        return self.update(
            orders_list=ctx.orders,
            positions_list=ctx.positions,
            tick=ctx.tick,
            orders_filtered=True,
            positions_filtered=True,
            atr=ctx.atr,
            action_collector=action_collector,
        )

    def update(
        self,
        orders_list=None,
        positions_list=None,
        tick=None,
        *,
        orders_filtered: bool = False,
        positions_filtered: bool = False,
        atr: float | None = None,
        action_collector: list | None = None,
    ):
        """核心巡检逻辑：支持双向网格与对标交易所模式"""
        self._action_collector = action_collector
        if not self.enabled:
            return
            
        now = time.time()
        if now < self.pause_until:
            return

        if tick is None:
            tick = self._get_tick()

        if not tick or tick.bid <= 0:
            self.pause_until = now + 5
            return

        if not self._is_market_open(tick):
            return

        spread_check = self.risk_manager.check_spread(
            bid=tick.bid,
            ask=tick.ask,
            max_spread_points=self.max_spread_points,
            point=self.point,
            extreme_cooldown=self.extreme_cooldown,
            now=now,
        )
        if spread_check.triggered:
            Logger.log(
                self.symbol,
                "FUSE",
                f"Spread Exceeded | Current={spread_check.spread/self.point:6.1f}pt > Max={self.max_spread_points:6.1f}pt | Cooldown={self.extreme_cooldown}s",
            )
            self.pause_until = spread_check.pause_until
            return

        self._maybe_adapt_params()

        atr_value = None
        if self.use_atr and not self.adaptive_enabled:
            if atr is not None:
                atr_value = atr
                self._last_atr_value = float(atr_value)
                self._last_atr_time = now
            elif self.datafeed is not None:
                atr_value = self.datafeed.get_atr(
                    self.symbol,
                    self._resolve_timeframe(),
                    self.atr_period,
                    self.atr_mode,
                    self.atr_smooth,
                    self.atr_update_seconds,
                )
                if atr_value is not None:
                    self._last_atr_value = float(atr_value)
                    self._last_atr_time = now
            else:
                atr_value = self._calculate_atr()

            # 优化说明：消除了重复的属性查找（self.use_atr, self.adaptive_enabled），内联合并逻辑
            if atr_value:
                self._apply_atr_targets(float(atr_value))

        mid_price = (tick.bid + tick.ask) / 2
        
        range_action = self.risk_manager.check_range(
            mid_price=mid_price,
            min_price=self.min_price,
            max_price=self.max_price,
            out_of_range_action=self.out_of_range_action,
        )
        if range_action == RangeAction.STOP:
            Logger.log(self.symbol, "STOP", f"mid {mid_price} out of range [{self.min_price}, {self.max_price}]")
            self.enabled = False
            self.clear_old_orders()
            return
        if range_action == RangeAction.FREEZE:
            return

        if orders_list is not None:
            if orders_filtered:
                my_orders = orders_list
            else:
                my_orders = [o for o in orders_list if o.magic == self.magic and o.symbol == self.symbol]
        else:
            orders = self._mt5_call(mt5.orders_get, symbol=self.symbol)
            my_orders = [o for o in orders if o.magic == self.magic] if orders else []
        
        if positions_list is not None:
            if positions_filtered:
                my_positions = positions_list
            else:
                my_positions = [p for p in positions_list if p.symbol == self.symbol and p.magic == self.magic]
        else:
            positions = self._mt5_call(mt5.positions_get, symbol=self.symbol)
            my_positions = [p for p in positions if p.symbol == self.symbol and p.magic == self.magic] if positions else []

        self._index_orders(my_orders)
            
        should_log_status = now - self._last_status_log_time > self._status_log_interval
        if should_log_status:
            float_profit = sum(p.profit for p in my_positions)
            pos_vol = sum(p.volume for p in my_positions)
            buy_orders = sum(len(v) for v in self.bid_orders.values())
            sell_orders = sum(len(v) for v in self.ask_orders.values())
            
            self._update_stats()
            
            price_width = max(12, self.digits + 8)
            step_width = max(8, self.digits + 4)
            step_prec = max(1, int(self.digits))
            atr_coef = 1.0
            if self.use_atr and self.base_step:
                atr_coef = self.step / self.base_step
            
            status_msg = (
                f"Magic={self.magic:04d} | "
                f"Price: {tick.bid:>{price_width}.{self.digits}f} / {tick.ask:>{price_width}.{self.digits}f} | "
                f"Position: {len(my_positions):2d}pos {pos_vol:6.2f}lot PnL:{float_profit:+10.2f} | "
                f"Orders: Buy={buy_orders:2d} Sell={sell_orders:2d} | "
                f"Grid: Step={self.step:>{step_width}.{step_prec}f} ATR={atr_coef:4.2f}x | "
                f"Stats: Long={self._stats['long_profitable_count']:3d}cnt/{self._stats['long_profitable_amount']:+10.2f} "
                f"Short={self._stats['short_profitable_count']:3d}cnt/{self._stats['short_profitable_amount']:+10.2f}"
            )
            Logger.log(self.symbol, "STATUS", status_msg)
            self._last_status_log_time = now

        # [修复 L-07] adaptive 模式下 min_price 每 K 线都会被动态覆盖，
        # 若用 min_price 作为初始锚点，adaptive 更新后锚点与价格范围会错位。
        # 改用 mid_price 作锚点：它位于当前价格范围中央，网格层级分布更合理。
        if self.anchor is None:
            self.anchor = mid_price if self.adaptive_enabled else self.min_price

        # [修复 L-05] neutral 模式下净多头同样可能超出 max_net_vol 上限，
        # 对冲管理器（做空对冲多头）在 neutral/long 两种模式下均适用。
        # short 模式的做多对冲属于镜像逻辑，当前实现不覆盖，在此明确排除。
        if self.hedge_enabled and self.mode in ("long", "neutral") and self.max_net_vol is not None:
            self._run_hedge_manager(my_positions, tick)

        positions_for_block = my_positions
        if self.mode == "long":
            if self.hedge_enabled:
                # [修复 L-12] 对冲模式下将全部持仓（含对冲空头）纳入阻塞层级，
                # 防止网格在对冲空头所在价位再挂买单，造成持仓混叠。
                positions_for_block = my_positions
            else:
                positions_for_block = [p for p in my_positions if p.type == mt5.POSITION_TYPE_BUY]
        elif self.mode == "short":
            positions_for_block = [p for p in my_positions if p.type == mt5.POSITION_TYPE_SELL]

        existing_positions_prices = {self._normalize_price(p.price_open) for p in positions_for_block}
        pos_k_set = set()
        if self.step > 0 and self.anchor is not None:
            pos_k_set = {round((p_price - self.anchor) / self.step) for p_price in existing_positions_prices}

        min_dist = max(self.stop_level, self.point * 10)

        target_buys, target_sells = self.grid_calculator.build_targets(
            anchor=self.anchor,
            step=self.step,
            min_price=self.min_price,
            max_price=self.max_price,
            bid=tick.bid,
            ask=tick.ask,
            buy_window=self.buy_window,
            sell_window=self.sell_window,
            mode=self.mode,
            recenter_steps=self.recenter_steps,
            min_dist=min_dist,
            blocked_k=pos_k_set,
        )

        if should_log_status and self.max_net_vol is not None and self.lot > 0:
            long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(
                my_positions, my_orders
            )
            liq_buffer = None
            try:
                account = mt5.account_info()
                if account:
                    equity = float(getattr(account, "equity", 0.0) or 0.0)
                    margin = float(getattr(account, "margin", 0.0) or 0.0)
                    so_mode = getattr(account, "margin_so_mode", None)
                    so_level = getattr(account, "margin_so_so", None)
                    if so_level is not None:
                        if so_mode == getattr(mt5, "ACCOUNT_STOP_OUT_PERCENT", None):
                            stopout_equity = margin * float(so_level) / 100.0
                        else:
                            stopout_equity = float(so_level)
                        liq_buffer = equity - stopout_equity
            except Exception:
                liq_buffer = None
            cap = float(self.max_net_vol)
            side = None
            current = 0.0
            remaining = 0.0

            if self.mode == "long":
                side = "buy"
                current = long_vol + pending_buy_vol
                remaining = cap - current
            elif self.mode == "short":
                side = "sell"
                current = short_vol + pending_sell_vol
                remaining = cap - current
            else:
                if net_vol >= 0:
                    side = "buy"
                    current = net_vol
                    remaining = cap - net_vol
                else:
                    side = "sell"
                    current = -net_vol
                    remaining = cap + net_vol

            # 优化说明：已移除内嵌函数 _cap_cell

            remark = ""
            last_target = None
            diff = None

            if remaining <= 0:
                max_orders = 0
            else:
                max_orders = int((remaining / self.lot) + 1e-9)

            if max_orders > 0:
                targets = target_buys if side == "buy" else target_sells
                is_window_limited = (len(targets) < max_orders)
                
                if is_window_limited and self.step > 0:
                    if side == "buy":
                        ref_price = targets[0] if targets else tick.ask
                        last_target = self._normalize_price(ref_price - self.step * (max_orders - 1))
                    else:
                        ref_price = targets[-1] if targets else tick.bid
                        last_target = self._normalize_price(ref_price + self.step * (max_orders - 1))
                    
                    remark = "窗口限制"
                elif targets:
                    if side == "buy":
                        idx = min(len(targets), max_orders) - 1
                        last_target = targets[idx]
                    else:
                        idx = -min(len(targets), max_orders)
                        last_target = targets[idx]
                else:
                    remark = "无目标"

                if last_target is not None:
                    if side == "buy":
                        diff = tick.ask - last_target
                    else:
                        diff = last_target - tick.bid
                    if last_target < self.min_price or last_target > self.max_price:
                        remark = "超范围" if not remark else f"{remark},超范围"

            side_cn = "买" if side == "buy" else "卖"
            cur_price = tick.ask if side == "buy" else tick.bid
            cur_str = f"{cur_price:.{self.digits}f}"
            expect_str = f"{last_target:.{self.digits}f}" if last_target is not None else "--"
            
            diff_str = "--"
            pct_str = "--"
            step_ratio_str = "--"
            if diff is not None:
                pct = 0.0
                if cur_price > 0:
                    pct = (diff / cur_price) * 100
                diff_str = f"{diff:+.{self.digits}f}"
                pct_str = f"{pct:+.2f}%"

            if cur_price > 0 and self.step > 0:
                step_ratio = (self.step / cur_price) * 100
                step_ratio_str = f"{step_ratio:.2f}%"

            pct_label = "跌幅:" if side == "buy" else "涨幅:"

            remark_str = remark if remark else ""
            liq_str = "--" if liq_buffer is None else f"{liq_buffer:+.2f}"

            cells = [
                _cap_cell("方向:", side_cn, 8),
                _cap_cell("上限:", f"{cap:.2f}", 12),
                _cap_cell("当前:", f"{current:.2f}", 12),
                _cap_cell("剩余:", f"{max(0.0, remaining):.2f}", 12),
                _cap_cell("手数:", f"{self.lot:.2f}", 10),
                _cap_cell("单数:", f"{max_orders}", 8),
                _cap_cell("爆仓额:", liq_str, 12),
                _cap_cell("现价:", cur_str, 16),
                _cap_cell("末档价:", expect_str, 16),
                _cap_cell("差值:", diff_str, 22),
                _cap_cell(pct_label, pct_str, 12),
                _cap_cell("步长%:", step_ratio_str, 12),
                _cap_cell("备注:", remark_str, 10),
            ]

            cap_msg = f"magic={self.magic} | CAP | " + " | ".join(cells)
            Logger.log(self.symbol, "STATUS", cap_msg)
        
        # A. TRIM (清理多余/超界挂单)
        if self.auto_trim:
            buy_to_keep, sell_to_keep = self._get_orders_to_keep(my_orders)
            target_set = set(target_buys) | set(target_sells)
            
            removed_tickets = set()
            for o in my_orders:
                if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
                    op = self._normalize_price(o.price_open)
                    is_buy = (o.type == mt5.ORDER_TYPE_BUY_LIMIT)
                    
                    # 优化说明：利用短路求值与卫语句取代原本啰嗦的 pass 分支，直接剔除了 should_remove 变量
                    if (is_buy and o in buy_to_keep) or (not is_buy and o in sell_to_keep):
                        continue
                        
                    if (op not in target_set) or (op < self.min_price) or (op > self.max_price) or \
                       (is_buy and self.mode == "short") or (not is_buy and self.mode == "long"):
                       
                        res = self._dispatch_request({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                        if res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            removed_tickets.add(o.ticket)
                            Logger.log(self.symbol, "TRIM", f"安全撤单(越界/模式冲突/目标外): {op}")

            if removed_tickets:
                my_orders = [o for o in my_orders if o.ticket not in removed_tickets]
                self._index_orders(my_orders)

        # B. 补单 (带库存风控)
        long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(my_positions, my_orders)
        long_pos_count = sum(1 for p in my_positions if p.type == mt5.POSITION_TYPE_BUY)
        short_pos_count = sum(1 for p in my_positions if p.type == mt5.POSITION_TYPE_SELL)

        mode_conflict = False
        if self.mode == "long":
            if (not self.hedge_enabled) and short_pos_count > 0:
                mode_conflict = True
        elif self.mode == "short":
            if long_pos_count > 0:
                mode_conflict = True

        if mode_conflict and should_log_status:
            Logger.log(
                self.symbol,
                "WARN",
                f"mode={self.mode} with opposite positions; skip new orders",
            )

        existing_buy_prices = set(self.bid_orders.keys())
        existing_sell_prices = set(self.ask_orders.keys())
        placed_count = 0
        placed_buy = 0
        placed_sell = 0
        skips_stats = {
            "buy": {"exist": 0, "near": 0, "pos": 0, "cap": 0, "risk": 0},
            "sell": {"exist": 0, "near": 0, "pos": 0, "cap": 0, "risk": 0},
        }

        if not mode_conflict:
            sides_to_process = [
                ("buy", target_buys, existing_buy_prices, tick.ask),
                ("sell", target_sells, existing_sell_prices, tick.bid),
            ]
            
            for side, targets, existing_prices, market_price in sides_to_process:
                result = self._place_side_targets(
                    side=side,
                    targets=targets,
                    existing_prices=existing_prices,
                    market_price=market_price,
                    min_dist=min_dist,
                    pos_k_set=pos_k_set,
                    existing_positions_prices=existing_positions_prices,
                    long_vol=long_vol,
                    short_vol=short_vol,
                    pending_buy_vol=pending_buy_vol,
                    pending_sell_vol=pending_sell_vol,
                    net_vol=net_vol,
                    long_pos_count=long_pos_count,
                    short_pos_count=short_pos_count,
                    placed_count=placed_count,
                )
                
                placed_count = result["placed_count"]
                pending_buy_vol = result["pending_buy_vol"]
                pending_sell_vol = result["pending_sell_vol"]
                net_vol = result["net_vol"]
                
                if side == "buy": placed_buy = result["placed_side"]
                else: placed_sell = result["placed_side"]

                skips_stats[side] = {
                    "exist": result["skip_exist"],
                    "near": result["skip_near"],
                    "pos": result["skip_pos"],
                    "cap": result["skip_cap"],
                    "risk": result["skip_risk"],
                }

        if should_log_status:
            # 优化说明：单行推导式完成解析，无需中途构建包含空字符的中间态列表
            skip_sections = [s for s in (_fmt_skip("B", skips_stats["buy"]), _fmt_skip("S", skips_stats["sell"])) if s]
            if skip_sections:
                Logger.log(
                    self.symbol,
                    "SKIP",
                    f"magic={self.magic} | targets B:{len(target_buys)} S:{len(target_sells)} | "
                    f"placed B:{placed_buy} S:{placed_sell} | "
                    f"min_dist={min_dist:.{self.digits}f} | "
                    + " ".join(skip_sections),
                )