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
    def _resolve_atr_reference(self, atr_value: float | None) -> float:
        if atr_value is not None and float(atr_value) > 0:
            return float(atr_value)
        cached = float(getattr(self, "_last_atr_value", 0.0) or 0.0)
        if cached > 0:
            return cached
        step = float(getattr(self, "step", 0.0) or 0.0)
        if step > 0:
            return step
        return max(float(getattr(self, "point", 0.0) or 0.0) * 10.0, 1e-6)

    def _handle_spread_fuse(self, *, tick, now: float, atr_reference: float) -> bool:
        spread = max(0.0, float(tick.ask - tick.bid))
        mid = max(float((tick.ask + tick.bid) * 0.5), float(self.point))
        atr_ref = max(float(atr_reference), float(self.point), 1e-9)

        abs_limit = None
        if self.max_spread_points is not None and self.max_spread_points > 0 and self.point > 0:
            abs_limit = float(self.max_spread_points) * float(self.point)

        rel_atr = spread / atr_ref
        rel_mid = spread / mid

        enter = (
            rel_atr >= float(getattr(self, "_spread_rel_atr_enter", 0.35))
            or rel_mid >= float(getattr(self, "_spread_rel_mid_enter", 0.003))
        )
        if abs_limit is not None and spread >= abs_limit:
            enter = True

        exit_ok = (
            rel_atr <= float(getattr(self, "_spread_rel_atr_exit", 0.25))
            and rel_mid <= float(getattr(self, "_spread_rel_mid_exit", 0.002))
        )
        if abs_limit is not None:
            exit_ok = exit_ok and spread <= abs_limit * 0.90

        if getattr(self, "_spread_fuse_active", False):
            if exit_ok:
                self._spread_fuse_active = False
                return False
            hold = max(1.0, float(getattr(self, "_spread_fuse_hold_seconds", 2.0) or 2.0))
            self.pause_until = max(self.pause_until, now + hold)
            return True

        if not enter:
            return False

        severity = 1.0
        if abs_limit is not None and abs_limit > 0:
            severity = max(severity, spread / abs_limit)
        severity = max(
            severity,
            rel_atr / max(float(getattr(self, "_spread_rel_atr_enter", 0.35)), 1e-9),
            rel_mid / max(float(getattr(self, "_spread_rel_mid_enter", 0.003)), 1e-9),
        )
        cooldown = float(self.extreme_cooldown) * min(3.0, max(1.0, severity))
        cooldown = max(cooldown, float(getattr(self, "_spread_fuse_hold_seconds", 2.0) or 2.0))

        self._spread_fuse_active = True
        self.pause_until = max(self.pause_until, now + cooldown)
        spread_pts = spread / max(float(self.point), 1e-9)
        abs_pts = (abs_limit / self.point) if (abs_limit is not None and self.point > 0) else 0.0
        Logger.log(
            self.symbol,
            "FUSE",
            (
                f"Spread fuse triggered | spread={spread_pts:.1f}pt/{abs_pts:.1f}pt | "
                f"rel_atr={rel_atr:.3f} rel_mid={rel_mid:.4%} | cooldown={cooldown:.1f}s"
            ),
        )
        return True

    def _compute_dynamic_windows(self, predicted_net_vol: float) -> tuple[int, int, float]:
        base_buy = max(0, int(getattr(self, "buy_window", self.window)))
        base_sell = max(0, int(getattr(self, "sell_window", self.window)))
        cap = float(self.max_net_vol or 0.0)
        if cap <= 0:
            return base_buy, base_sell, 0.0

        pressure = max(-1.5, min(1.5, float(predicted_net_vol) / cap))
        if pressure >= 0:
            buy_scale = max(0.2, 1.0 - 0.8 * pressure)
            sell_scale = min(2.5, 1.0 + 0.6 * pressure)
        else:
            p = abs(pressure)
            buy_scale = min(2.5, 1.0 + 0.6 * p)
            sell_scale = max(0.2, 1.0 - 0.8 * p)

        dynamic_buy = max(0, int(round(base_buy * buy_scale)))
        dynamic_sell = max(0, int(round(base_sell * sell_scale)))

        if self.mode == "long":
            dynamic_sell = 0
        elif self.mode == "short":
            dynamic_buy = 0

        return dynamic_buy, dynamic_sell, pressure

    def _rank_targets_by_utility(
        self,
        *,
        side: str,
        targets: list[float],
        tick,
        predicted_net_vol: float,
        atr_reference: float,
    ) -> list[float]:
        if not targets:
            return targets

        cap = float(self.max_net_vol or 0.0)
        spread = max(0.0, float(tick.ask - tick.bid))
        lot = self._normalize_volume(self.lot)
        ranked = []
        for price in targets:
            p_fill = self._estimate_fill_probability(
                side=side,
                price=float(price),
                bid=float(tick.bid),
                ask=float(tick.ask),
                atr=atr_reference,
            )
            projected = float(predicted_net_vol) + (lot * p_fill if side == "buy" else -lot * p_fill)
            directional_pressure = 0.0
            if cap > 0:
                directional_pressure = projected / cap
                if side == "sell":
                    directional_pressure *= -1.0
                directional_pressure = max(0.0, min(2.0, directional_pressure))

            reward = float(self.tp_dist) * p_fill
            distance = (tick.ask - price) if side == "buy" else (price - tick.bid)
            distance_penalty = max(0.0, float(distance) - float(self.step) * 0.5)
            cost_penalty = spread + 0.2 * distance_penalty
            risk_penalty = float(self.step) * 0.7 * directional_pressure
            utility = reward - 0.35 * cost_penalty - risk_penalty
            ranked.append((utility, price))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [price for _, price in ranked]

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

        atr_reference = self._resolve_atr_reference(atr_value)
        if self._handle_spread_fuse(tick=tick, now=now, atr_reference=atr_reference):
            return

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

        # [P-02] O(N) 一次性预处理持仓聚合，后续全程引用缓存结果，避免约 9 次重复遍历
        _float_profit = 0.0
        _long_pos_count = 0
        _short_pos_count = 0
        for _p in my_positions:
            _float_profit += _p.profit
            if _p.type == mt5.POSITION_TYPE_BUY:
                _long_pos_count += 1
            else:
                _short_pos_count += 1

        # [P-09] 预计算 exposure，供 CAP 段与补单段共用，消除两次独立 _calc_exposure 调用
        long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(
            my_positions, my_orders
        )
        predicted_net_vol = self._calc_predicted_net_exposure(
            my_positions,
            my_orders,
            tick=tick,
            atr=atr_reference,
        )
        _pos_vol = long_vol + short_vol

        should_log_status = now - self._last_status_log_time > self._status_log_interval
        if should_log_status:
            float_profit = _float_profit
            pos_vol = _pos_vol
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
                f"PredNet={predicted_net_vol:+7.2f} | "
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

        # 运行时锚点平移：价格偏离超过 recenter_steps*step 且冷却结束后重置 anchor。
        if self.anchor is not None and self.step > 0:
            recenter_steps = int(getattr(self, "recenter_steps", 0) or 0)
            recenter_cooldown = float(getattr(self, "recenter_cooldown", 0.0) or 0.0)
            if recenter_steps > 0:
                drift = abs(mid_price - self.anchor)
                trigger = recenter_steps * self.step
                last_recenter_time = float(getattr(self, "_last_recenter_time", 0.0) or 0.0)
                if drift >= trigger and (now - last_recenter_time) >= recenter_cooldown:
                    old_anchor = self.anchor
                    self.anchor = mid_price
                    self._last_recenter_time = now
                    Logger.log(
                        self.symbol,
                        "RECENTER",
                        (
                            f"magic={self.magic} | "
                            f"{old_anchor:.{self.digits}f} -> {self.anchor:.{self.digits}f} | "
                            f"drift={drift:.{self.digits}f} trigger={trigger:.{self.digits}f}"
                        ),
                    )
                    # 重置历史挂单窗口，避免旧 anchor 的网格残留。
                    self.clear_old_orders()

        # [修复 L-05] neutral 模式下净多头同样可能超出 max_net_vol 上限，
        # 对冲管理器（做空对冲多头）在 neutral/long 两种模式下均适用。
        # short 模式的做多对冲属于镜像逻辑，当前实现不覆盖，在此明确排除。
        if self.hedge_enabled and self.mode in ("long", "neutral") and self.max_net_vol is not None:
            self._run_hedge_manager(my_positions, tick, predicted_net_vol=predicted_net_vol)

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
        dynamic_buy_window, dynamic_sell_window, inventory_pressure = self._compute_dynamic_windows(predicted_net_vol)

        target_buys, target_sells = self.grid_calculator.build_targets(
            anchor=self.anchor,
            step=self.step,
            min_price=self.min_price,
            max_price=self.max_price,
            bid=tick.bid,
            ask=tick.ask,
            buy_window=dynamic_buy_window,
            sell_window=dynamic_sell_window,
            mode=self.mode,
            min_dist=min_dist,
            blocked_k=pos_k_set,
        )

        target_buys = self._rank_targets_by_utility(
            side="buy",
            targets=target_buys,
            tick=tick,
            predicted_net_vol=predicted_net_vol,
            atr_reference=atr_reference,
        )
        target_sells = self._rank_targets_by_utility(
            side="sell",
            targets=target_sells,
            tick=tick,
            predicted_net_vol=predicted_net_vol,
            atr_reference=atr_reference,
        )

        if should_log_status and self.max_net_vol is not None:
            Logger.log(
                self.symbol,
                "STATUS",
                (
                    f"magic={self.magic} | WindowDyn B:{dynamic_buy_window}/{int(self.buy_window)} "
                    f"S:{dynamic_sell_window}/{int(self.sell_window)} | pressure={inventory_pressure:+.2f}"
                ),
            )

        if should_log_status and self.max_net_vol is not None and self.lot > 0:
            # [P-09] 复用入口处已预计算的 exposure，无需重复调用 _calc_exposure
            liq_buffer = None
            try:
                account = self._mt5_call(mt5.account_info)
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
                if predicted_net_vol >= 0:
                    side = "buy"
                    current = predicted_net_vol
                    remaining = cap - predicted_net_vol
                else:
                    side = "sell"
                    current = -predicted_net_vol
                    remaining = cap + predicted_net_vol

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
            # [P-08] 用 set + update 替代 set(a)|set(b)，避免创建两个中间集合再合并
            target_set = set(target_buys)
            target_set.update(target_sells)
            
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
                        if (
                            res is not None
                            and (not getattr(res, "queued", False))
                            and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
                        ):
                            removed_tickets.add(o.ticket)
                            Logger.log(self.symbol, "TRIM", f"安全撤单(越界/模式冲突/目标外): {op}")

            if removed_tickets:
                my_orders = [o for o in my_orders if o.ticket not in removed_tickets]
                self._index_orders(my_orders)
                # [P-09] 挂单集合变化时同步更新 exposure，其余情况复用入口缓存值
                long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol = self._calc_exposure(
                    my_positions, my_orders
                )
                predicted_net_vol = self._calc_predicted_net_exposure(
                    my_positions,
                    my_orders,
                    tick=tick,
                    atr=atr_reference,
                )

        # B. 补单 (带库存风控)
        # [P-02/P-09] 使用入口预计算的 exposure 和持仓计数，无需额外遍历
        long_pos_count = _long_pos_count
        short_pos_count = _short_pos_count

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
                    predicted_net_vol=predicted_net_vol,
                    tick=tick,
                    atr_for_prob=atr_reference,
                    long_pos_count=long_pos_count,
                    short_pos_count=short_pos_count,
                    placed_count=placed_count,
                )
                
                placed_count = result["placed_count"]
                pending_buy_vol = result["pending_buy_vol"]
                pending_sell_vol = result["pending_sell_vol"]
                net_vol = result["net_vol"]
                predicted_net_vol = result["predicted_net_vol"]
                
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
