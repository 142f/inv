# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import time
from core.logger import Logger
from .runtime_mixins import iter_filling_candidates

class GridOrdersMixin:
    def _order_check(self, request):
        request = self._prepare_request(request)
        if request is None:
            return None
        try:
            result = self._mt5_call(mt5.order_check, request)
        except Exception as exc:
            Logger.log(self.symbol, "ERROR", f"order_check exception: {exc}")
            return None

        if result is None:
            last_error = mt5.last_error()
            Logger.log(self.symbol, "ERROR", f"order_check returned None. Error: {last_error}")
            return None

        retcode = getattr(result, "retcode", None)
        comment = getattr(result, "comment", "") or ""
        msg = (
            f"Check OK | RetCode={retcode} {comment} | "
            f"Type={request.get('type')} Price={request.get('price')} "
            f"Vol={request.get('volume')} Fill={request.get('type_filling', '')}"
        )
        Logger.log(self.symbol, "ORDER_CHECK", msg)
        return result

    def _send_with_fillings(self, request):
        last_result = None
        for mode in iter_filling_candidates(self.filling_mode):
            req = dict(request)
            req['type_filling'] = mode
            check = self._order_check(req)
            # [修复 L-10] 利用 order_check 结果提前拦截不可恢复的错误，避免发出必然失败的订单请求。
            # retcode=0 表示预检通过；retcode=10030 是 filling 模式不兼容（继续尝试下一种）；
            # 其他非零错误（如余额不足 10019）说明订单本身不合法，直接中止。
            if check is not None:
                check_rc = getattr(check, "retcode", 0)
                if check_rc not in (0, 10030):
                    Logger.log(self.symbol, "WARN",
                        f"order_check 预检拒绝 RetCode={check_rc} {getattr(check, 'comment', '')}，中止下单")
                    return None
            last_result = self._dispatch_request(req)
            if last_result is None:
                return None
            if last_result.retcode != 10030:
                return last_result

        # [修复 L-03] 所有 filling 模式均返回 10030（不支持的填充方式）时，
        # 直接返回最后一次结果而非重发不含 type_filling 的请求（必然再次失败）。
        Logger.log(self.symbol, "WARN",
            f"All filling modes rejected (10030) for price={request.get('price')} "
            f"type={request.get('type')}; giving up.")
        return last_result

    def _index_orders(self, my_orders):
        self.bid_orders = {}
        self.ask_orders = {}
        for o in my_orders:
            if o.type == mt5.ORDER_TYPE_BUY_LIMIT:
                op = self._normalize_price(o.price_open)
                self.bid_orders.setdefault(op, []).append(o)
            elif o.type == mt5.ORDER_TYPE_SELL_LIMIT:
                op = self._normalize_price(o.price_open)
                self.ask_orders.setdefault(op, []).append(o)

    def _place_limit_order(self, side: str, price: float):
        try:
            is_buy = side == "buy"
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
            label = "BUY" if is_buy else "SELL"

            price = self._normalize_price(price)
            tp = self._normalize_price(price + self.tp_dist if is_buy else price - self.tp_dist)
            sl = None
            if self.sl_dist and self.sl_dist > 0:
                dist = max(self.sl_dist, self.stop_level, self.point)
                sl = price - dist if is_buy else price + dist
                sl = self._normalize_price(sl)
                if (is_buy and sl >= price) or ((not is_buy) and sl <= price):
                    sl = None
            vol = self._normalize_volume(self.lot)
            atr_coef = 1.0
            if self.use_atr and self.base_step:
                atr_coef = self.step / self.base_step
            price_width = max(12, self.digits + 9)

            sl_str = f"{sl:>{price_width}.{self.digits}f}" if sl is not None else f"{'--':>{price_width}}"

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": self.symbol,
                "volume": vol,
                "type": order_type,
                "price": price,
                "tp": tp,
                "deviation": 20,
                "magic": self.magic,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            if sl is not None:
                request["sl"] = sl

            if self._action_collector is not None:
                self._queue_action(request)
                Logger.log(
                    self.symbol,
                    "ORDER_SENT",
                    f"{label} LIMIT | Price={price:>{price_width}.{self.digits}f} TP={tp:>{price_width}.{self.digits}f} SL={sl_str} | Magic={self.magic:04d} | ATR={atr_coef:.2f}x (queued)",
                )
                return True

            result = self._send_with_fillings(request)
            if result is None:
                last_error = mt5.last_error()
                Logger.log(self.symbol, "ERROR", f"order_send returned None. Error: {last_error}")
                return None

            if result.retcode not in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                if result.retcode == 10004:  # REQUOTE
                    # [修复 L-11] 重试前刷新 tick 并校验价格仍与市场保持合法距离，
                    # 避免在剧烈行情下用旧价格反复 requote。
                    fresh_tick = self._get_tick()
                    if fresh_tick is None or fresh_tick.bid <= 0:
                        Logger.log(self.symbol, "WARN", "Requote 后无法获取最新 tick，放弃下单")
                        return None
                    min_dist = max(self.stop_level, self.point * 10)
                    too_close = (
                        (is_buy and (fresh_tick.ask - price) < min_dist) or
                        (not is_buy and (price - fresh_tick.bid) < min_dist)
                    )
                    if too_close:
                        Logger.log(self.symbol, "WARN",
                            f"Requote 后价格 {price:.{self.digits}f} 距市场过近 (<{min_dist:.{self.digits}f})，放弃下单")
                        return None
                    Logger.log(self.symbol, "WARN", "Requote，已确认价格合法，重试中...")
                    time.sleep(0.1)
                    result = self._send_with_fillings(request)
                    if result is None:
                        last_error = mt5.last_error()
                        Logger.log(self.symbol, "ERROR", f"order_send returned None after requote. Error: {last_error}")
                        return None
                    if result.retcode in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                        Logger.log(
                            self.symbol,
                            "ORDER_SENT",
                            f"{label} LIMIT | Price={price:>{price_width}.{self.digits}f} TP={tp:>{price_width}.{self.digits}f} SL={sl_str} | Magic={self.magic:04d} | ATR={atr_coef:.2f}x (retry)",
                        )
                        return result.order

                self._handle_order_error(result.retcode, getattr(result, "comment", ""), price)
                return None

            Logger.log(
                self.symbol,
                "ORDER_SENT",
                f"{label} LIMIT | Price={price:>{price_width}.{self.digits}f} TP={tp:>{price_width}.{self.digits}f} SL={sl_str} | Magic={self.magic:04d} | ATR={atr_coef:.2f}x",
            )
            return result.order

        except Exception as exc:
            Logger.log(self.symbol, "EXCEPTION", f"order exception: {exc}")
            self.pause_until = max(self.pause_until, time.time() + 2)
            return None

    def _place_buy_order(self, price):
        return self._place_limit_order("buy", price)

    def _place_sell_order(self, price):
        return self._place_limit_order("sell", price)

    def _handle_order_error(self, retcode, comment, price):
        """统一处理订单错误"""
        if retcode == 10018: # MARKET_CLOSED
            Logger.log(self.symbol, "SLEEP", "市场休市，暂停运行 5 分钟")
            self.pause_until = time.time() + 300
        elif retcode == 10017: # TRADE_DISABLED
            Logger.log(self.symbol, 'WARN', 'Trade disabled. Check terminal/account/symbol permissions.')
            self.pause_until = time.time() + 60
        elif retcode == 10027: # CLIENT_DISABLES_AT
            Logger.log(self.symbol, "CRITICAL", "MT5 终端 '自动交易' (Algo Trading) 未开启！请在 MT5 软件上方点击 'Algo Trading' 按钮。")
            self.enabled = False # 必须停止，否则会死循环
        elif retcode == 10004: # REQUOTE
            Logger.log(self.symbol, "WARN", "价格重新报价 (Requote)，稍后重试")
            self.pause_until = time.time() + 1
        elif retcode == 10013: # INVALID_REQUEST
            Logger.log(self.symbol, "ERROR", "无效请求参数")
            self.enabled = False # 致命错误，停止策略
        elif retcode == 10014: # INVALID_VOLUME
            Logger.log(self.symbol, "ERROR", "无效手数")
            self.enabled = False
        else:
            Logger.log(self.symbol, "ORDER_FAIL", f"RetCode={retcode} | Price={price:.{self.digits}f} | Reason: {comment}")
            # 通用错误暂停 5 秒，防止刷屏
            self.pause_until = time.time() + 5

    def _get_orders_to_keep(self, my_orders):
        buy_orders = [o for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT]
        sell_orders = [o for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT]
        buy_orders.sort(key=lambda x: x.price_open, reverse=True)
        sell_orders.sort(key=lambda x: x.price_open)
        buy_to_keep = buy_orders[:self.window] if self.window > 0 else []
        sell_to_keep = sell_orders[:self.window] if self.window > 0 else []
        return buy_to_keep, sell_to_keep

    def clear_old_orders(self):
        """启动时清理旧网格挂单，保留价格最近的window数量个订单"""
        orders = self._mt5_call(mt5.orders_get, symbol=self.symbol)
        my_orders = [o for o in orders if o.magic == self.magic] if orders else []

        if not my_orders:
            Logger.log(self.symbol, "CLEANUP", f"Magic={self.magic:04d} | 无历史挂单，跳过清理")
            return

        tick = self._get_tick()
        if tick is None:
            return

        buy_to_keep, sell_to_keep = self._get_orders_to_keep(my_orders)

        # [修复 L-08] 改用 ticket 集合做 O(1) 查找，避免依赖对象身份比较（可能误判）
        buy_keep_tickets = {o.ticket for o in buy_to_keep}
        sell_keep_tickets = {o.ticket for o in sell_to_keep}
        buy_to_remove = [o for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT and o.ticket not in buy_keep_tickets]
        sell_to_remove = [o for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT and o.ticket not in sell_keep_tickets]

        # 删除超出窗口的订单
        for o in buy_to_remove + sell_to_remove:
            res = self._dispatch_request({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            if res is None:
                continue
            if res.retcode == 10018: # MARKET_CLOSED
                Logger.log(self.symbol, "WARN", "市场休市，无法撤单，暂停运行 5 分钟")
                self.pause_until = time.time() + 300
                return

        Logger.log(
            self.symbol,
            "CLEANUP",
            f"Magic={self.magic:04d} | 历史挂单清理完成 | 删除买单{len(buy_to_remove)}个 卖单{len(sell_to_remove)}个"
            f"，保留买单{len(buy_to_keep)}个 卖单{len(sell_to_keep)}个",
        )

    # ------------------------
    # Risk / caps helpers
    # ------------------------
    def _calc_exposure(self, my_positions, my_orders):
        """计算当前持仓和挂单的敞口情况。
        
        Args:
            my_positions: 本策略的持仓列表
            my_orders: 本策略的挂单列表
            
        Returns:
            tuple: (long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol)
        """
        # 持仓量计算
        long_vol = sum(p.volume for p in my_positions if p.type == mt5.POSITION_TYPE_BUY)
        short_vol = sum(p.volume for p in my_positions if p.type == mt5.POSITION_TYPE_SELL)

        # 挂单量计算 - 使用 volume_current（当前剩余量）而非 volume_initial（初始量）
        # 因为部分成交的订单应该只计算剩余部分
        pending_buy_vol = sum(
            getattr(o, 'volume_current', o.volume_initial) 
            for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT
        )
        pending_sell_vol = sum(
            getattr(o, 'volume_current', o.volume_initial) 
            for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT
        )

        # 净持仓 = (多头 + 待买) - (空头 + 待卖)
        net_vol = (long_vol + pending_buy_vol) - (short_vol + pending_sell_vol)

        return long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol

    def _allow_side(self, side, long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol,
                     *, long_pos_count: int = 0, short_pos_count: int = 0):
        return self.risk_manager.check_inventory_limits(
            long_vol=long_vol,
            short_vol=short_vol,
            pending_buy_vol=pending_buy_vol,
            pending_sell_vol=pending_sell_vol,
            net_vol=net_vol,
            lot=self.lot,
            side=side,
            mode=self.mode,
            max_net_vol=self.max_net_vol,
            max_long_vol=self.max_long_vol,
            max_short_vol=self.max_short_vol,
            max_long_pos=self.max_long_pos,
            max_short_pos=self.max_short_pos,
            long_pos_count=long_pos_count,
            short_pos_count=short_pos_count,
            hedge_enabled=self.hedge_enabled,
        )

    def _has_duplicate_position_level(self, price, pos_k_set, existing_positions_prices):
        if pos_k_set:
            level = round((price - self.anchor) / self.step)
            return level in pos_k_set

        for p_price in existing_positions_prices:
            if abs(p_price - price) < (self.step * 0.1):
                return True
        return False

    def _place_side_targets(
        self,
        *,
        side: str,
        targets,
        existing_prices,
        market_price: float,
        min_dist: float,
        pos_k_set,
        existing_positions_prices,
        long_vol: float,
        short_vol: float,
        pending_buy_vol: float,
        pending_sell_vol: float,
        net_vol: float,
        long_pos_count: int,
        short_pos_count: int,
        placed_count: int,
    ):
        skip_exist = 0
        skip_near = 0
        skip_pos = 0
        skip_cap = 0
        skip_risk = 0
        placed_side = 0

        for price in targets:
            if placed_count >= self.max_new_orders_per_update:
                skip_cap += 1
                break
            if price in existing_prices:
                skip_exist += 1
                continue
            if abs(price - market_price) < min_dist:
                skip_near += 1
                continue

            if self._has_duplicate_position_level(price, pos_k_set, existing_positions_prices):
                skip_pos += 1
                continue

            if not self._allow_side(
                side,
                long_vol,
                short_vol,
                pending_buy_vol,
                pending_sell_vol,
                net_vol,
                long_pos_count=long_pos_count,
                short_pos_count=short_pos_count,
            ):
                skip_risk += 1
                break

            placed = self._place_buy_order(price) if side == "buy" else self._place_sell_order(price)
            if placed:
                placed_count += 1
                placed_side += 1
                if side == "buy":
                    pending_buy_vol += self.lot
                    net_vol += self.lot
                else:
                    pending_sell_vol += self.lot
                    net_vol -= self.lot

        return {
            "placed_count": placed_count,
            "placed_side": placed_side,
            "pending_buy_vol": pending_buy_vol,
            "pending_sell_vol": pending_sell_vol,
            "net_vol": net_vol,
            "skip_exist": skip_exist,
            "skip_near": skip_near,
            "skip_pos": skip_pos,
            "skip_cap": skip_cap,
            "skip_risk": skip_risk,
        }