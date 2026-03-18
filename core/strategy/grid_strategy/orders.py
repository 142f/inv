# Auto-extracted from core/strategy_lib.py during refactor.
import MetaTrader5 as mt5
import time
from core.logger import Logger
from core.strategy.grid.exposure_model import calc_predicted_net_exposure, estimate_fill_probability
from .runtime_mixins import iter_filling_candidates

class GridOrdersMixin:
    # [P-01] 璁句负 True 浠ュ紑鍚?order_check 棰勬锛圖EBUG 鐢ㄩ€旓級锛?
    # 榛樿鍏抽棴浠ュ噺灏戠害 50% 鐨?MT5 API 璋冪敤寮€閿€銆?
    _DEBUG_ORDER_CHECK = False

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
            # [P-01] order_check 浠呭湪 DEBUG 妯″紡涓嬭皟鐢紝鍑忓皯绾?50% 鐨?MT5 API 寮€閿€銆?
            # 涓嶅彲鎭㈠閿欒锛堝浣欓涓嶈冻锛変細鐢?_dispatch_request 杩斿洖鐨?retcode 鎹曡幏锛?
            # 璋冪敤鏂?_place_limit_order 浼氶€氳繃 _handle_order_error 缁熶竴澶勭悊銆?
            if self._DEBUG_ORDER_CHECK:
                check = self._order_check(req)
                if check is not None:
                    check_rc = getattr(check, "retcode", 0)
                    if check_rc not in (0, 10030):
                        Logger.log(
                            self.symbol,
                            "WARN",
                            f"order_check rejected RetCode={check_rc} {getattr(check, 'comment', '')}; skip sending",
                        )
                        return None
            last_result = self._dispatch_request(req)
            if last_result is None:
                return None
            if last_result.retcode != 10030:
                return last_result

        # [淇 L-03] 鎵€鏈?filling 妯″紡鍧囪繑鍥?10030锛堜笉鏀寔鐨勫～鍏呮柟寮忥級鏃讹紝
        # 鐩存帴杩斿洖鏈€鍚庝竴娆＄粨鏋滆€岄潪閲嶅彂涓嶅惈 type_filling 鐨勮姹傦紙蹇呯劧鍐嶆澶辫触锛夈€?
        Logger.log(self.symbol, "WARN",
            f"All filling modes rejected (10030) for price={request.get('price')} "
            f"type={request.get('type')}; giving up.")
        return last_result

    def _index_orders(self, my_orders):
        # [P-12] 浠呭湪鎸傚崟闆嗗悎锛坱icket 闆嗭級瀹為檯鍙樺寲鏃堕噸寤虹储寮曪紝閬垮厤姣?tick 鍏ㄩ噺閲嶅缓
        new_tickets = frozenset(o.ticket for o in my_orders)
        if getattr(self, '_last_order_tickets', None) == new_tickets:
            return
        self._last_order_tickets = new_tickets
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
            # [P-10] atr_coef / price_width / sl_str 寤惰繜鍒扮湡姝ｉ渶瑕佽緭鍑烘棩蹇楁椂鎵嶈绠?

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
                _pw = max(12, self.digits + 9)
                _ac = (self.step / self.base_step) if (self.use_atr and self.base_step) else 1.0
                _sl = f"{sl:>{_pw}.{self.digits}f}" if sl is not None else f"{'--':>{_pw}}"
                Logger.log(
                    self.symbol,
                    "ORDER_SENT",
                    f"{label} LIMIT | Price={price:>{_pw}.{self.digits}f} TP={tp:>{_pw}.{self.digits}f} SL={_sl} | Magic={self.magic:04d} | ATR={_ac:.2f}x (queued)",
                )
                return True

            result = self._send_with_fillings(request)
            if result is None:
                last_error = mt5.last_error()
                Logger.log(self.symbol, "ERROR", f"order_send returned None. Error: {last_error}")
                return None

            if result.retcode not in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                if result.retcode == 10004:  # REQUOTE
                    # [淇 L-11] 閲嶈瘯鍓嶅埛鏂?tick 骞舵牎楠屼环鏍间粛涓庡競鍦轰繚鎸佸悎娉曡窛绂伙紝
                    # 閬垮厤鍦ㄥ墽鐑堣鎯呬笅鐢ㄦ棫浠锋牸鍙嶅 requote銆?
                    fresh_tick = self._get_tick()
                    if fresh_tick is None or fresh_tick.bid <= 0:
                        Logger.log(self.symbol, "WARN", "No valid tick after requote; skip order")
                        return None
                    min_dist = max(self.stop_level, self.point * 10)
                    too_close = (
                        (is_buy and (fresh_tick.ask - price) < min_dist) or
                        (not is_buy and (price - fresh_tick.bid) < min_dist)
                    )
                    if too_close:
                        Logger.log(
                            self.symbol,
                            "WARN",
                            f"Price {price:.{self.digits}f} too close after requote (<{min_dist:.{self.digits}f}); skip",
                        )
                        return None
                    Logger.log(self.symbol, "WARN", "Requote锛屽凡纭浠锋牸鍚堟硶锛岄噸璇曚腑...")
                    time.sleep(0.1)
                    result = self._send_with_fillings(request)
                    if result is None:
                        last_error = mt5.last_error()
                        Logger.log(self.symbol, "ERROR", f"order_send returned None after requote. Error: {last_error}")
                        return None
                    if result.retcode in [mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED]:
                        self._on_order_submit_success()
                        _pw = max(12, self.digits + 9)
                        _ac = (self.step / self.base_step) if (self.use_atr and self.base_step) else 1.0
                        _sl = f"{sl:>{_pw}.{self.digits}f}" if sl is not None else f"{'--':>{_pw}}"
                        Logger.log(
                            self.symbol,
                            "ORDER_SENT",
                            f"{label} LIMIT | Price={price:>{_pw}.{self.digits}f} TP={tp:>{_pw}.{self.digits}f} SL={_sl} | Magic={self.magic:04d} | ATR={_ac:.2f}x (retry)",
                        )
                        return result.order

                self._handle_order_error(result.retcode, getattr(result, "comment", ""), price)
                return None

            self._on_order_submit_success()
            _pw = max(12, self.digits + 9)
            _ac = (self.step / self.base_step) if (self.use_atr and self.base_step) else 1.0
            _sl = f"{sl:>{_pw}.{self.digits}f}" if sl is not None else f"{'--':>{_pw}}"
            Logger.log(
                self.symbol,
                "ORDER_SENT",
                f"{label} LIMIT | Price={price:>{_pw}.{self.digits}f} TP={tp:>{_pw}.{self.digits}f} SL={_sl} | Magic={self.magic:04d} | ATR={_ac:.2f}x",
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

    def _on_order_submit_success(self):
        self._transient_reject_streak = 0

    def _transient_reject_backoff(self):
        streak = int(getattr(self, "_transient_reject_streak", 0) or 0) + 1
        self._transient_reject_streak = streak
        backoff_seconds = min(30.0, 0.5 * (2 ** min(streak, 6)))
        self.pause_until = max(self.pause_until, time.time() + backoff_seconds)
        return streak, backoff_seconds

    def _handle_order_error(self, retcode, comment, price):
        """Centralized trade retcode handler with transient backoff."""
        try:
            price_text = f"{float(price):.{self.digits}f}"
        except (TypeError, ValueError):
            price_text = "--"

        if retcode == 10018:  # MARKET_CLOSED
            Logger.log(self.symbol, "SLEEP", "Market closed; pause 300s")
            self.pause_until = time.time() + 300
            self._transient_reject_streak = 0
        elif retcode == 10017:  # TRADE_DISABLED
            Logger.log(self.symbol, "WARN", "Trade disabled. Check terminal/account/symbol permissions.")
            self.pause_until = time.time() + 60
            self._transient_reject_streak = 0
        elif retcode == 10027:  # CLIENT_DISABLES_AT
            Logger.log(self.symbol, "CRITICAL", "MT5 terminal Algo Trading is disabled; strategy stopped")
            self.enabled = False
            self._transient_reject_streak = 0
        elif retcode == 10004:  # REQUOTE
            Logger.log(self.symbol, "WARN", "Requote received; retry later")
            self.pause_until = time.time() + 1
            self._transient_reject_streak = 0
        elif retcode in (10006, 10024):  # REJECT / TOO_MANY_REQUESTS
            streak, backoff_seconds = self._transient_reject_backoff()
            Logger.log(
                self.symbol,
                "WARN",
                (
                    f"RetCode={retcode} | Price={price_text} | "
                    f"Reason: {comment or 'request rejected'} | "
                    f"backoff={backoff_seconds:.1f}s streak={streak}"
                ),
            )
        elif retcode == 10013:  # INVALID_REQUEST
            Logger.log(self.symbol, "ERROR", "Invalid trade request; strategy disabled")
            self.enabled = False
            self._transient_reject_streak = 0
        elif retcode == 10014:  # INVALID_VOLUME
            Logger.log(self.symbol, "ERROR", "Invalid volume; strategy disabled")
            self.enabled = False
            self._transient_reject_streak = 0
        else:
            Logger.log(self.symbol, "ORDER_FAIL", f"RetCode={retcode} | Price={price_text} | Reason: {comment}")
            self.pause_until = time.time() + 5
            self._transient_reject_streak = 0

    def _get_orders_to_keep(self, my_orders):
        buy_orders = [o for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT]
        sell_orders = [o for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT]
        buy_orders.sort(key=lambda x: x.price_open, reverse=True)
        sell_orders.sort(key=lambda x: x.price_open)
        buy_keep_n = max(0, int(getattr(self, "buy_window", self.window)))
        sell_keep_n = max(0, int(getattr(self, "sell_window", self.window)))

        # Enforce directional mode when trimming existing pending orders.
        mode = str(getattr(self, "mode", "neutral") or "neutral").strip().lower()
        if mode == "long":
            sell_keep_n = 0
        elif mode == "short":
            buy_keep_n = 0

        buy_to_keep = buy_orders[:buy_keep_n]
        sell_to_keep = sell_orders[:sell_keep_n]
        return buy_to_keep, sell_to_keep

    def clear_old_orders(self, force_all: bool = False):
        """Clear stale pending orders for this strategy.

        force_all=True or strategy disabled: remove all strategy-owned pending orders.
        Otherwise keep only the configured window (`buy_window`/`sell_window`).
        """
        orders = self._mt5_call(mt5.orders_get, symbol=self.symbol)
        my_orders = [o for o in orders if o.magic == self.magic] if orders else []

        if not my_orders:
            Logger.log(self.symbol, "CLEANUP", f"Magic={self.magic:04d} | no pending orders")
            return

        if force_all or (not self.enabled):
            buy_to_keep, sell_to_keep = [], []
        else:
            buy_to_keep, sell_to_keep = self._get_orders_to_keep(my_orders)

        buy_keep_tickets = {o.ticket for o in buy_to_keep}
        sell_keep_tickets = {o.ticket for o in sell_to_keep}
        buy_to_remove = [o for o in my_orders if o.type == mt5.ORDER_TYPE_BUY_LIMIT and o.ticket not in buy_keep_tickets]
        sell_to_remove = [o for o in my_orders if o.type == mt5.ORDER_TYPE_SELL_LIMIT and o.ticket not in sell_keep_tickets]

        removed_buy = 0
        removed_sell = 0
        failed_remove = 0
        queued_remove = 0
        for o in buy_to_remove + sell_to_remove:
            res = self._dispatch_request({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            if res is None:
                failed_remove += 1
                continue
            if getattr(res, "queued", False):
                queued_remove += 1
                continue
            if res.retcode == 10018:  # MARKET_CLOSED
                Logger.log(self.symbol, "WARN", "Market closed while removing pending orders; pause 300s")
                self.pause_until = time.time() + 300
                return
            if res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                if o.type == mt5.ORDER_TYPE_BUY_LIMIT:
                    removed_buy += 1
                elif o.type == mt5.ORDER_TYPE_SELL_LIMIT:
                    removed_sell += 1
            else:
                failed_remove += 1

        Logger.log(
            self.symbol,
            "CLEANUP",
            f"Magic={self.magic:04d} | "
            f"{'[FORCE_ALL] ' if force_all else ''}"
            f"{'[DISABLED] ' if not self.enabled else ''}"
            f"Cleanup finished | removed buy={removed_buy} sell={removed_sell} | "
            f"kept buy={len(buy_to_keep)} sell={len(sell_to_keep)} | "
            f"fail={failed_remove} queued={queued_remove}",
        )

    # ------------------------
    # Risk / caps helpers
    # ------------------------
    def _estimate_fill_probability(self, *, side: str, price: float, bid: float, ask: float, atr: float | None = None) -> float:
        return estimate_fill_probability(
            side=side,
            price=float(price),
            bid=float(bid),
            ask=float(ask),
            point=float(getattr(self, "point", 0.0) or 0.0),
            step=float(getattr(self, "step", 0.0) or 0.0),
            atr=atr,
        )

    def _calc_exposure(self, my_positions, my_orders):
        """璁＄畻褰撳墠鎸佷粨鍜屾寕鍗曠殑鏁炲彛鎯呭喌銆?
        
        Args:
            my_positions: 鏈瓥鐣ョ殑鎸佷粨鍒楄〃
            my_orders: 鏈瓥鐣ョ殑鎸傚崟鍒楄〃
            
        Returns:
            tuple: (long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol)
        """
        # [P-03] 鍗曟閬嶅巻鍚屾椂绱鎵€鏈夌淮搴︼紝鏇夸唬鍘熸潵 4 娆＄嫭绔嬬敓鎴愬櫒閬嶅巻
        long_vol = 0.0
        short_vol = 0.0
        for p in my_positions:
            if p.type == mt5.POSITION_TYPE_BUY:
                long_vol += p.volume
            else:
                short_vol += p.volume

        # 鎸傚崟閲忚绠?- 浣跨敤 volume_current锛堝綋鍓嶅墿浣欓噺锛夎€岄潪 volume_initial锛堝垵濮嬮噺锛?
        # 鍥犱负閮ㄥ垎鎴愪氦鐨勮鍗曞簲璇ュ彧璁＄畻鍓╀綑閮ㄥ垎
        pending_buy_vol = 0.0
        pending_sell_vol = 0.0
        for o in my_orders:
            vol = getattr(o, 'volume_current', o.volume_initial)
            if o.type == mt5.ORDER_TYPE_BUY_LIMIT:
                pending_buy_vol += vol
            elif o.type == mt5.ORDER_TYPE_SELL_LIMIT:
                pending_sell_vol += vol

        # 鍑€鎸佷粨 = (澶氬ご + 寰呬拱) - (绌哄ご + 寰呭崠)
        net_vol = (long_vol + pending_buy_vol) - (short_vol + pending_sell_vol)

        return long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol

    def _calc_predicted_net_exposure(self, my_positions, my_orders, *, tick, atr: float | None = None) -> float:
        return calc_predicted_net_exposure(
            positions=my_positions,
            orders=my_orders,
            tick=tick,
            point=float(getattr(self, "point", 0.0) or 0.0),
            step=float(getattr(self, "step", 0.0) or 0.0),
            atr=atr,
        )

    def _allow_side(self, side, long_vol, short_vol, pending_buy_vol, pending_sell_vol, net_vol,
                     *, long_pos_count: int = 0, short_pos_count: int = 0, net_lot: float | None = None):
        # Keep risk checks aligned with actual order volume sent to broker.
        effective_lot = self._normalize_volume(self.lot)
        return self.risk_manager.check_inventory_limits(
            long_vol=long_vol,
            short_vol=short_vol,
            pending_buy_vol=pending_buy_vol,
            pending_sell_vol=pending_sell_vol,
            net_vol=net_vol,
            lot=effective_lot,
            net_lot=net_lot,
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

    @staticmethod
    def _has_nearby_pending_price(price: float, existing_prices, tolerance: float) -> bool:
        tol = max(0.0, float(tolerance))
        if tol <= 0:
            return False
        for exist_price in existing_prices:
            if abs(float(exist_price) - float(price)) <= tol:
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
        predicted_net_vol: float,
        tick,
        atr_for_prob: float | None,
        long_pos_count: int,
        short_pos_count: int,
        placed_count: int,
    ):
        skip_exist = 0
        skip_dupnear = 0
        skip_near = 0
        skip_pos = 0
        skip_cap = 0
        skip_risk = 0
        placed_side = 0
        effective_lot = self._normalize_volume(self.lot)
        near_order_tol = max(
            float(getattr(self, "point", 0.0) or 0.0) * 2.0,
            float(getattr(self, "step", 0.0) or 0.0) * 0.25,
        )

        for price in targets:
            if placed_count >= self.max_new_orders_per_update:
                skip_cap += 1
                break
            if price in existing_prices:
                skip_exist += 1
                continue
            if self._has_nearby_pending_price(price, existing_prices, near_order_tol):
                skip_dupnear += 1
                continue
            if abs(price - market_price) < min_dist:
                skip_near += 1
                continue

            if self._has_duplicate_position_level(price, pos_k_set, existing_positions_prices):
                skip_pos += 1
                continue

            fill_prob = self._estimate_fill_probability(
                side=side,
                price=float(price),
                bid=float(tick.bid),
                ask=float(tick.ask),
                atr=atr_for_prob,
            )
            net_lot = effective_lot * fill_prob

            if not self._allow_side(
                side,
                long_vol,
                short_vol,
                pending_buy_vol,
                pending_sell_vol,
                predicted_net_vol,
                long_pos_count=long_pos_count,
                short_pos_count=short_pos_count,
                net_lot=net_lot,
            ):
                skip_risk += 1
                break

            placed = self._place_buy_order(price) if side == "buy" else self._place_sell_order(price)
            if placed:
                placed_count += 1
                placed_side += 1
                existing_prices.add(price)
                if side == "buy":
                    pending_buy_vol += effective_lot
                    net_vol += effective_lot
                    predicted_net_vol += net_lot
                else:
                    pending_sell_vol += effective_lot
                    net_vol -= effective_lot
                    predicted_net_vol -= net_lot
            elif placed is None:
                # 涓嬪崟澶辫触鏃剁粓姝㈠綋鍓嶈竟琛ュ崟锛岄伩鍏嶅悓涓€杞腑杩炵画鎷掑崟
                skip_risk += 1
                break

        return {
            "placed_count": placed_count,
            "placed_side": placed_side,
            "pending_buy_vol": pending_buy_vol,
            "pending_sell_vol": pending_sell_vol,
            "net_vol": net_vol,
            "predicted_net_vol": predicted_net_vol,
            "skip_exist": skip_exist,
            "skip_dupnear": skip_dupnear,
            "skip_near": skip_near,
            "skip_pos": skip_pos,
            "skip_cap": skip_cap,
            "skip_risk": skip_risk,
        }
