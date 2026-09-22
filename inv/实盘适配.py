"""MT5 适配器：真实账户快照驱动共享策略、仓位和风控。"""
from dataclasses import asdict,replace
from datetime import datetime, timedelta, timezone
import hashlib
import math
import threading
import time
from pathlib import Path

from core.execution import ExecutionService, NativeExecutionResult, DONE_RETCODE
from inv.domain.models import Bar, SignalType, SymbolSpec
from inv.仓位风控 import RiskPolicy, RiskState, PositionSizer


class ModuleBroker:
    """将既有 MT5 模块接入公共执行服务，不访问旧密钥文件。"""
    def __init__(self,module):
        self.module=module
        self.lock=threading.RLock()

    def order_send(self,request):
        return self.module.order_send(request)


class MT5Adapter:
    def __init__(self,module,store,*,enabled=False):
        self.module,self.store=module,store
        self.service=ExecutionService(ModuleBroker(module),mode='live',live_enabled=enabled,state_repository=store)
        self.commands=store.get('pending_commands') or {}

    def submit(self,key,request,strategy_id):
        # 短评论令牌供券商历史对账，完整身份保留在本地状态。
        token='inv'+hashlib.sha256(key.encode()).hexdigest()[:20]
        request={**request,'comment':token}
        if key in self.commands and self.commands[key]['status']=='unknown':
            # 包含“预写入后、发送前”崩溃窗口；未确定时宁可暂停也不重发。
            return NativeExecutionResult(-1,'命令状态未知，等待券商对账')
        if key not in self.commands:
            check=self.module.order_check(request)
            if check is None or int(check.retcode)!=0:
                return NativeExecutionResult(-1,'券商预检未通过')
            self.commands[key]=dict(request=request,strategy_id=strategy_id,token=token,
                                    time=time.time(),status='unknown')
            self.store.set('pending_commands',self.commands)
            self.store.flush()
        result=self.service.submit_native(request,strategy_id=strategy_id,idempotency_key=key)
        if result.retcode in (10008,10009,10010):
            self.commands[key]['status']='accepted'
        elif result.retcode not in (-1,10012,10031,10027):
            self.commands[key]['status']='rejected'
        self.store.set('pending_commands',self.commands)
        self.store.flush()
        return result

    def reconcile(self):
        unknown=[(key,row) for key,row in self.commands.items() if row['status']=='unknown']
        if not unknown: return True
        start=datetime.fromtimestamp(min(row['time'] for _,row in unknown)-3600,timezone.utc)
        end=datetime.now(timezone.utc)+timedelta(seconds=10)
        positions=self.module.positions_get()
        orders=self.module.orders_get()
        deals=self.module.history_deals_get(start,end)
        history=self.module.history_orders_get(start,end)
        if any(value is None for value in (positions,orders,deals,history)): return False
        for key,row in unknown:
            matches=[item for item in (*positions,*orders,*deals,*history)
                     if getattr(item,'comment','')==row['token'] and getattr(item,'magic',None)==row['request'].get('magic')]
            # 查询为空不证明未成交，未知命令持续阻止开仓。
            if matches:
                result=NativeExecutionResult(DONE_RETCODE,'券商历史已确认',order=int(matches[0].ticket))
                self.service.reconcile_confirmed(row['request'],row['strategy_id'],result,idempotency_key=key)
                row['status']='accepted'
        self.store.set('pending_commands',self.commands)
        self.store.flush()
        return all(row['status']!='unknown' for row in self.commands.values())


class LiveRunner:
    def __init__(self,module,adapter,strategy,settings,*,timeframe='D1',policy=None,allocation=1.0):
        self.mt5,self.adapter,self.strategy,self.settings=module,adapter,strategy,settings
        self.timeframe=timeframe
        self.allocation=allocation
        self.bar_key=f'last_bar:{settings.strategy_id}:{timeframe}'
        self.policy=policy or RiskPolicy()
        self.sizer=PositionSizer(self.policy)
        saved=adapter.store.get('risk')
        self.risk=RiskState(**saved) if saved else None
        self.last_bar=adapter.store.get(self.bar_key)
        self.grid_slots=adapter.store.get('grid_slots') or {}

    def cycle(self):
        mt5=self.mt5
        account=mt5.account_info()
        if account is None or not math.isfinite(account.equity) or account.equity<=0:
            raise RuntimeError('账户权益不可用，停止新增订单')
        if getattr(account,'currency','USD')!='USD': raise RuntimeError('当前公开研究配置仅支持美元账户')
        info=mt5.symbol_info(self.settings.symbol)
        tick=mt5.symbol_info_tick(self.settings.symbol)
        positions=mt5.positions_get()
        orders=mt5.orders_get()
        if info is None or tick is None or positions is None or orders is None:
            raise RuntimeError('账户或行情快照不完整')
        if time.time()-tick.time>30 or tick.bid<=0 or tick.ask<tick.bid:
            raise RuntimeError('报价无效或过期')
        spec=SymbolSpec(self.settings.symbol,info.trade_tick_size,info.volume_min,info.volume_max,info.volume_step,
                        contract_size=info.trade_contract_size,point=info.point,digits=info.digits,
                        tick_size=info.trade_tick_size,tick_value=info.trade_tick_value,
                        stops_level=info.trade_stops_level,freeze_level=info.trade_freeze_level)
        strategy_id=self.settings.strategy_id
        service=self.adapter.service
        service.set_live_permission(strategy_id,True)
        service.begin_cycle(strategy_id,100)
        reconciled=self.adapter.reconcile()
        saved=self.adapter.store.get('risk')
        if saved: self.risk=RiskState(**saved)
        if self.risk is None: self.risk=RiskState(account.equity,account.equity)
        allowed=self.risk.assess(account.equity,tick.time,self.policy)
        allowed=allowed and account.margin <= account.equity*self.policy.max_margin_usage
        own=[p for p in positions if p.symbol==self.settings.symbol and p.magic==self.settings.magic]
        own_orders=[o for o in orders if o.symbol==self.settings.symbol and o.magic==self.settings.magic]
        own_exposure=sum(p.volume*spec.contract_size*tick.ask for p in own)
        own_exposure+=sum(o.volume_current*spec.contract_size*tick.ask for o in own_orders)
        allowed=allowed and own_exposure<=account.equity*self.allocation*self.policy.max_asset_exposure
        gross=0.
        for item in (*positions,*orders):
            item_info=mt5.symbol_info(item.symbol)
            item_tick=mt5.symbol_info_tick(item.symbol)
            if item_info is None or item_tick is None: raise RuntimeError('组合敞口无法核对')
            volume=getattr(item,'volume',getattr(item,'volume_current',0))
            gross+=volume*item_info.trade_contract_size*item_tick.ask
        allowed=allowed and gross<=account.equity*self.policy.max_portfolio_exposure
        emergency=bool(self.adapter.store.get('emergency_stop')) or Path('运行状态/紧急停止').exists()
        if not allowed or emergency:
            self._cancel(own_orders,strategy_id,int(tick.time))
            for pos in own: self._close(pos,tick,strategy_id,int(tick.time))
            self._persist()
            return dict(status='风险暂停',positions=len(own))
        if not reconciled:
            self._persist()
            return dict(status='等待订单对账')
        timeframe=getattr(mt5,'TIMEFRAME_'+self.timeframe)
        rates=mt5.copy_rates_from_pos(self.settings.symbol,timeframe,1,400)
        if rates is None or len(rates)<120: raise RuntimeError('已收盘历史不足')
        bars=[Bar(float(r['time']),float(r['open']),float(r['high']),float(r['low']),float(r['close']),
                  spread=float(r['spread'])*info.point,volume=float(r['tick_volume'])) for r in rates]
        bar_time=bars[-1].timestamp
        if self.last_bar is not None and bar_time<=self.last_bar:
            return dict(status='等待下一根收盘')
        if any(p.symbol==self.settings.symbol and p.magic!=self.settings.magic for p in positions):
            raise RuntimeError('同品种存在其他策略持仓，拒绝混合净持仓归属')
        if 'grid' in self.strategy.family and getattr(account,'margin_mode',-1)!=2:
            raise RuntimeError('多层双向网格需要 MT5 对冲持仓账户')
        self.strategy.state.position=sum(p.volume*(1 if p.type==0 else -1) for p in own)
        signals=self.strategy.compute_signals(bars,len(bars)-1,self.settings)
        self._cancel(own_orders,strategy_id,bar_time)
        asset_gross=sum(p.volume*spec.contract_size*tick.ask for p in own)
        reserved_margin=account.margin
        # 移动保护单使用已收盘 ATR，与纸面模式相同；只向盈利方向收紧。
        atr=self.strategy._calc_atr(bars,len(bars)-1,self.settings.atr_period)
        if atr:
            for pos in own:
                desired=bars[-1].close-(1 if pos.type==0 else -1)*atr*3
                sl=max(pos.sl,desired) if pos.type==0 else min(pos.sl or desired,desired)
                sl=spec.ticks_to_price(spec.price_to_ticks(sl))
                distance=max(info.trade_stops_level,info.trade_freeze_level)*info.point
                valid=(sl<tick.bid-distance) if pos.type==0 else (sl>tick.ask+distance)
                if valid and abs(sl-pos.sl)>=spec.price_tick:
                    self.adapter.submit(f'{strategy_id}:{bar_time}:trail:{pos.ticket}',
                        dict(action=6,position=pos.ticket,symbol=pos.symbol,magic=self.settings.magic,sl=sl,tp=pos.tp),strategy_id)
        for index,signal in enumerate(signals):
            if signal.signal_type==SignalType.HOLD: continue
            if signal.signal_type in (SignalType.CLOSE_LONG,SignalType.CLOSE_SHORT):
                target=0 if signal.signal_type==SignalType.CLOSE_LONG else 1
                for pos in own:
                    if pos.type==target: self._close(pos,tick,strategy_id,bar_time)
                continue
            buy=signal.signal_type==SignalType.BUY
            limit=signal.metadata.get('order_type')=='limit'
            if limit and any(self.grid_slots.get(getattr(p,'comment',''))==signal.metadata.get('grid_key') for p in own):
                continue
            if not limit and own:
                for pos in own:
                    if pos.type != (0 if buy else 1): self._close(pos,tick,strategy_id,bar_time)
                # 已有持仓变化留待下一快照确认，不基于乐观平仓结果立即翻仓。
                continue
            price=signal.price if limit else (tick.ask if buy else tick.bid)
            price=spec.ticks_to_price(spec.price_to_ticks(price))
            if signal.stop_loss is None: continue
            sl=spec.ticks_to_price(spec.price_to_ticks(signal.stop_loss))
            tp=spec.ticks_to_price(spec.price_to_ticks(signal.take_profit)) if signal.take_profit else 0.
            distance=max(info.trade_stops_level,info.trade_freeze_level)*info.point
            if (buy and sl>=price-distance) or (not buy and sl<=price+distance): continue
            if tp and ((buy and tp<=price+distance) or (not buy and tp>=price-distance)): continue
            if limit and ((buy and price>=tick.ask-distance) or (not buy and price<=tick.bid+distance)): continue
            volume=self.sizer.size(replace(signal,stop_loss=sl,take_profit=tp),price,account.equity*self.allocation,spec,
                                   asset_gross,self.risk.peak*self.allocation,gross*self.allocation,
                                   risk_fraction=self.settings.risk_per_trade)
            if volume<=0: continue
            order_type=(2 if buy else 3) if limit else (0 if buy else 1)
            margin=mt5.order_calc_margin(0 if buy else 1,self.settings.symbol,volume,price)
            if margin is None or margin+reserved_margin>account.equity*self.policy.max_margin_usage: continue
            request=dict(action=5 if limit else 1,symbol=self.settings.symbol,magic=self.settings.magic,
                         type=order_type,volume=volume,price=price,sl=sl,tp=tp,deviation=20,
                         type_time=0,type_filling=2 if limit else self._filling(info))
            key=f'{strategy_id}:{bar_time}:{index}'
            result=self.adapter.submit(key,request,strategy_id)
            if result.retcode not in (10008,10009,10010): break
            if limit:
                self.grid_slots['inv'+hashlib.sha256(key.encode()).hexdigest()[:20]]=signal.metadata.get('grid_key')
            asset_gross+=volume*price*spec.contract_size
            gross+=volume*price*spec.contract_size
            reserved_margin+=margin
        self.last_bar=bar_time
        self._persist()
        return dict(status='周期完成',signals=len(signals))

    @staticmethod
    def _filling(info):
        return 0 if info.filling_mode & 1 else (1 if info.filling_mode & 2 else 2)

    def _cancel(self,orders,strategy_id,bar_time):
        for order in orders:
            result=self.adapter.submit(f'{strategy_id}:{bar_time}:cancel:{order.ticket}',
                dict(action=8,order=order.ticket,symbol=order.symbol,magic=self.settings.magic),strategy_id)
            if result.retcode not in (10008,10009,10010): raise RuntimeError('撤单尚未确认，停止重新布单')

    def _close(self,pos,tick,strategy_id,bar_time):
        info=self.mt5.symbol_info(pos.symbol)
        return self.adapter.submit(f'{strategy_id}:{bar_time}:close:{pos.ticket}',
            dict(action=1,position=pos.ticket,symbol=pos.symbol,magic=self.settings.magic,
                 type=1 if pos.type==0 else 0,volume=pos.volume,price=tick.bid if pos.type==0 else tick.ask,
                 deviation=20,type_filling=self._filling(info)),strategy_id)

    def _persist(self):
        self.adapter.store.set('risk',asdict(self.risk))
        self.adapter.store.set(self.bar_key,self.last_bar)
        self.adapter.store.set('grid_slots',self.grid_slots)
        self.adapter.store.flush()
