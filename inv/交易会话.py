"""逐根行情驱动的共享纸面账本；回测只是离线驱动同一会话。"""
from dataclasses import asdict, replace
from math import isfinite
from statistics import median
from inv.domain.models import Bar, SignalType, SymbolSpec, BacktestResult
from inv.backtest.metrics import calculate_metrics
from inv.仓位风控 import RiskPolicy, RiskState, PositionSizer


class TradingSession:
    def __init__(self, engine, strategy, settings, spec=None, *, policy=None, warmup=0):
        from inv.backtest.engine import _ExecutionState
        self.engine, self.strategy, self.settings = engine, strategy, settings
        self.spec = spec or SymbolSpec(settings.symbol, .01, .01, 10000, .01,
                                      contract_size=100, tick_size=.01, tick_value=1)
        self.multiplier = self.spec.tick_value/self.spec.tick_size if self.spec.tick_value > 0 else self.spec.contract_size
        self.policy = policy or engine.risk_policy
        self.sizer = PositionSizer(self.policy)
        self.risk = RiskState(engine.initial_equity, engine.initial_equity)
        self.state = _ExecutionState(engine.initial_equity)
        self.state.fee_contract_size = self.spec.contract_size
        self.positions = []
        self.pending = []
        self.bars = []
        self.trades, self.attribution, self.events = [], [], []
        self.equity, self.times = [engine.initial_equity], []
        self.warmup = warmup
        self.funding = self.spread_cost = 0.0
        self.max_leverage = self.max_margin = 0.0
        self.liquidations = self.exposed_bars = 0
        self.last_time = None
        self.strategy.reset_state()

    def value(self, price):
        return self.state.cash + sum(p.direction*p.volume*price*self.multiplier for p in self.positions)

    def gross(self, price):
        return sum(p.volume*price*self.spec.contract_size for p in self.positions)

    def close(self, pos, price, timestamp, reason):
        self.state.position = pos
        before = len(self.trades)
        self.engine._close_position(self.state, price, timestamp, self.trades, self.multiplier)
        self.positions.remove(pos)
        self.state.position = None
        for trade in self.trades[before:]:
            self.strategy.update_state(trade)
            self.attribution.append(dict(strategy=getattr(pos, 'strategy_name', self.strategy.strategy_id),
                                         reason=reason, net_pnl=trade.net_pnl, side=trade.side))

    def _protect(self, bar, timestamp, gap_only=False):
        # 同根触及止损止盈取止损；已有头寸跳空先于新开仓处理。
        check = Bar(bar.timestamp, bar.open, bar.open, bar.open, bar.open) if gap_only else bar
        for pos in list(self.positions):
            self.state.position = pos
            start = len(self.trades)
            target = pos.take_profit
            if not gap_only and pos.entry_time == timestamp and getattr(pos, 'is_limit', False):
                # 新限价单无法证明先成交后触及止盈，保守推迟止盈到下一根。
                pos.take_profit = None
            self.engine._check_protection(self.state, check, timestamp, self.trades, self.multiplier)
            pos.take_profit = target
            if self.state.position is None:
                self.positions.remove(pos)
                for trade in self.trades[start:]:
                    self.strategy.update_state(trade)
                    self.attribution.append(dict(strategy=getattr(pos, 'strategy_name', self.strategy.strategy_id),
                                                 reason='保护退出', net_pnl=trade.net_pnl, side=trade.side))
        self.state.position = None

    def on_bar(self, bar):
        timestamp = self.engine._to_timestamp(bar.timestamp)
        if self.last_time is not None and timestamp <= self.last_time:
            raise ValueError('行情必须严格按时间递增')
        if not all(isfinite(v) and v > 0 for v in (bar.open, bar.high, bar.low, bar.close)) or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError('行情 OHLC 无效')
        if len(self.bars) < self.warmup:
            self.bars.append(bar)
            self.last_time = timestamp
            return
        self.times.append(timestamp)
        if self.last_time is not None and self.positions:
            days = (timestamp-self.last_time)/86400
            for pos in self.positions:
                cost = pos.volume*bar.open*self.spec.contract_size*self.engine.funding_daily*days
                self.state.cash -= cost
                pos.entry_cost += cost
                self.funding += cost
        # 跳空先检查维持保证金，不能用已经失效的止损伪装成未发生强平。
        if self.positions and self.value(bar.open) <= self.gross(bar.open)/self.policy.max_leverage*self.policy.maintenance_margin:
            self.liquidations += 1
            self.risk.halted,self.risk.reason=True,'跳空强平'
            for pos in list(self.positions): self.close(pos,bar.open,timestamp,'跳空强平')
        self._protect(bar, timestamp, gap_only=True)
        permitted = self.risk.assess(self.value(bar.open), timestamp, self.policy)
        if not permitted:
            self.pending = []
            for pos in list(self.positions):
                self.close(pos, bar.open, timestamp, self.risk.reason)
        for signal in self.pending:
            if signal.signal_type == SignalType.HOLD:
                continue
            if signal.signal_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                wanted = 'long' if signal.signal_type == SignalType.CLOSE_LONG else 'short'
                for pos in list(self.positions):
                    if pos.side == wanted:
                        self.close(pos, bar.open, timestamp, '信号退出')
                continue
            side = 'long' if signal.signal_type == SignalType.BUY else 'short'
            is_limit = signal.metadata.get('order_type') == 'limit'
            price = bar.open
            if is_limit:
                if (side == 'long' and bar.low > signal.price) or (side == 'short' and bar.high < signal.price):
                    continue
                # 限价买不高于委托价，卖不低于委托价。
                price = min(price, signal.price) if side == 'long' else max(price, signal.price)
                if any(getattr(p, 'grid_key', None) == signal.metadata.get('grid_key') for p in self.positions):
                    continue
            else:
                reversed_position=False
                for pos in list(self.positions):
                    if pos.side != side:
                        self.close(pos, price, timestamp, '反向信号')
                        reversed_position=True
                # 与真实执行一致：平仓后等待下一账户快照，不在同一事件乐观翻仓。
                if reversed_position:
                    continue
                if self.positions:
                    continue
            sl = signal.stop_loss
            if sl is None:
                sl = price*(.95 if side == 'long' else 1.05)
            if (side == 'long' and sl >= price) or (side == 'short' and sl <= price):
                continue
            if signal.take_profit is not None and ((side == 'long' and signal.take_profit <= price) or (side == 'short' and signal.take_profit >= price)):
                continue
            sl=self.spec.ticks_to_price(self.spec.price_to_ticks(sl))
            tp=self.spec.ticks_to_price(self.spec.price_to_ticks(signal.take_profit)) if signal.take_profit else None
            if (side=='long' and sl>=price) or (side=='short' and sl<=price):
                continue
            if tp is not None and ((side=='long' and tp<=price) or (side=='short' and tp>=price)):
                continue
            signal = replace(signal, stop_loss=sl,take_profit=tp)
            equity = self.value(price)
            volume = self.sizer.size(signal, price, equity, self.spec, self.gross(price), self.risk.peak,
                                     risk_fraction=self.settings.risk_per_trade)
            volume = self.spec.normalize_volume(volume*self.engine.partial_fill_ratio)
            if volume <= 0 or not self.risk.assess(equity, timestamp, self.policy):
                continue
            self.state.position = None
            self.engine._execute_signal(self.state, replace(signal, volume=volume), price, timestamp, self.trades, self.multiplier)
            pos = self.state.position
            if pos:
                spread = max(bar.spread, price*self.engine.spread_pct)
                # 双边点差成本在建仓时预留，平仓不再次重复扣除。
                cost = spread*volume*self.multiplier
                self.state.cash -= cost
                pos.entry_cost += cost
                self.spread_cost += cost
                pos.strategy_name = signal.metadata.get('strategy', self.strategy.strategy_id)
                pos.grid_key = signal.metadata.get('grid_key')
                pos.is_limit = is_limit
                self.positions.append(pos)
                current_equity=self.value(price)
                leverage=self.gross(price)/max(current_equity,1e-12)
                self.max_leverage=max(self.max_leverage,leverage)
                self.max_margin=max(self.max_margin,leverage/self.policy.max_leverage)
                self.events.append(dict(time=timestamp, side=side, volume=volume, price=price,
                                        strategy=pos.strategy_name, confidence=signal.confidence))
            self.state.position = None
        self.pending = []
        # 缺少逐笔路径时先检验整根最不利价的保证金，再检验保护单。
        if self.positions:
            worst = min(self.value(bar.low), self.value(bar.high))
            margin = max(self.gross(bar.low), self.gross(bar.high))/self.policy.max_leverage
            if worst <= margin*self.policy.maintenance_margin:
                price = bar.low if self.value(bar.low) <= self.value(bar.high) else bar.high
                self.liquidations += 1
                self.risk.halted, self.risk.reason = True, '保守盘中强平'
                for pos in list(self.positions):
                    self.close(pos, price, timestamp, '强平')
        self._protect(bar, timestamp)
        equity = self.value(bar.close)
        gross = self.gross(bar.close)
        self.max_leverage = max(self.max_leverage, gross/max(equity, 1e-12))
        self.max_margin = max(self.max_margin, gross/self.policy.max_leverage/max(equity, 1e-12))
        self.exposed_bars += bool(self.positions)
        allowed = self.risk.assess(equity, timestamp, self.policy)
        if not allowed:
            for pos in list(self.positions):
                self.close(pos, bar.close, timestamp, self.risk.reason)
            equity = self.value(bar.close)
        self.equity.append(equity)
        self.bars.append(bar)
        self.last_time = timestamp
        self.strategy.state.position = sum(p.direction*p.volume for p in self.positions)
        self.strategy.state.entry_price = self.positions[0].entry_price if self.positions else 0
        # 策略只得到当前已闭合数据，无法索引未来。
        if allowed:
            self.pending = list(self.strategy.compute_signals(self.bars, len(self.bars)-1, self.settings))
        # 移动止损在收盘后更新，最早下一根生效。
        atr = self.strategy._calc_atr(self.bars, len(self.bars)-1, self.settings.atr_period)
        if atr and self.engine.trailing_atr > 0:
            for pos in self.positions:
                candidate = bar.close-pos.direction*atr*self.engine.trailing_atr
                if pos.side == 'long':
                    pos.stop_loss = max(pos.stop_loss or candidate, candidate)
                else:
                    pos.stop_loss = min(pos.stop_loss or candidate, candidate)

    def result(self, *, liquidate=True):
        if liquidate and self.bars:
            for pos in list(self.positions):
                self.close(pos, self.bars[-1].close, self.last_time, '期末平仓')
            self.equity[-1] = self.state.cash
            self.strategy.state.position = 0
        # 以 UTC 每日权益计算指标，避免把小时收益误当作日收益。
        daily = {}
        for timestamp, value in zip(self.times, self.equity[1:]):
            daily[int(timestamp//86400)] = value
        if daily:
            days = range(min(daily), max(daily)+1)
            last = self.engine.initial_equity
            curve = [last]
            for day in days:
                last = daily.get(day, last)
                curve.append(last)
        else:
            curve = self.equity
        metrics = calculate_metrics(curve, self.trades, periods_per_year=365)
        # 最大回撤同时覆盖盘中各根收盘，不被每日采样掩盖。
        from inv.backtest.metrics import _max_drawdown
        dd = _max_drawdown(self.equity)
        metrics = replace(metrics, max_drawdown=dd,
                          calmar_ratio=metrics.annualized_return/dd if dd else 0)
        return BacktestResult(tuple(self.equity), tuple(self.trades), metrics, dict(
            final_equity=self.equity[-1], initial_equity=self.engine.initial_equity,
            gross_commission=self.state.gross_commission, gross_slippage=self.state.gross_slippage,
            funding=self.funding, spread_cost=self.spread_cost,
            total_costs=self.state.gross_commission+self.state.gross_slippage+self.funding+self.spread_cost,
            liquidation_count=self.liquidations, max_leverage=self.max_leverage, max_margin_usage=self.max_margin,
            exposure=self.exposed_bars/max(1, len(self.times)), risk_halted=self.risk.halted,
            risk_reason=self.risk.reason, attribution=self.attribution, events=self.events,
            timestamps=self.times, daily_equity=daily, max_position_volume=max((t.volume for t in self.trades), default=0),
            average_holding_seconds=sum(t.exit_time-t.entry_time for t in self.trades)/max(1,len(self.trades))))

    def checkpoint(self):
        """仅保存当前会话生成的状态，调用者负责原子持久化。"""
        payload={name:getattr(self,name) for name in ('equity','times','events','attribution','warmup','funding',
            'spread_cost','max_leverage','max_margin','liquidations','exposed_bars','last_time')}
        payload.update(state=asdict(self.state),risk=asdict(self.risk),
            positions=[vars(pos).copy() for pos in self.positions],trades=[asdict(t) for t in self.trades],
            bars=[{**asdict(b),'timestamp':self.engine._to_timestamp(b.timestamp)} for b in self.bars],
            pending=[{**asdict(s),'timestamp':s.timestamp.isoformat(),'signal_type':s.signal_type.value} for s in self.pending],
            strategy_state=asdict(self.strategy.state),fixed_center=getattr(self.strategy,'fixed_center',None))
        return payload

    @classmethod
    def restore(cls,payload,engine,strategy,settings,spec=None):
        from datetime import datetime
        from inv.backtest.engine import Position,_ExecutionState
        from inv.domain.models import Signal,Trade
        from inv.strategy.base import StrategyState
        session=cls(engine,strategy,settings,spec)
        for name in ('equity','times','events','attribution','warmup','funding','spread_cost','max_leverage',
                     'max_margin','liquidations','exposed_bars','last_time'):
            setattr(session,name,payload[name])
        session.state=_ExecutionState(**{**payload['state'],'position':None})
        session.risk=RiskState(**payload['risk'])
        for row in payload['positions']:
            fields={key:value for key,value in row.items() if key in Position.__dataclass_fields__}
            pos=Position(**fields)
            for key,value in row.items(): setattr(pos,key,value)
            session.positions.append(pos)
        session.bars=[Bar(**row) for row in payload['bars']]
        session.trades=[Trade(**row) for row in payload['trades']]
        session.pending=[Signal(**{**row,'timestamp':datetime.fromisoformat(row['timestamp']),
                                   'signal_type':SignalType(row['signal_type'])}) for row in payload['pending']]
        strategy.state=StrategyState(**payload['strategy_state'])
        if hasattr(strategy,'fixed_center'): strategy.fixed_center=payload['fixed_center']
        return session
