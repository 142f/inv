"""5000美元单账户固定预算组合，按真实手数步长逐笔模拟。"""
from dataclasses import asdict, replace
import heapq
from inv.backtest.engine import BacktestEngine
from inv.backtest.metrics import calculate_metrics, _max_drawdown
from inv.domain.settings import StrategySettings
from inv.strategy.行情自适应 import AdaptiveStrategy,causal_features
from inv.交易会话 import TradingSession
from inv.仓位风控 import RiskPolicy,RiskState


def portfolio_replay(history,selections,*,start_time,end_time,spec_factory,cost_factory):
    """每资产预留1/3预算，闲置预算为现金；组合风控可同时阻止所有资产新增风险。"""
    initial=5000.
    sessions={}
    heap=[]
    idle=0.
    for asset in ('BTC','ETH','XAU'):
        choice=selections.get(asset)
        if not choice:
            idle+=initial/3
            continue
        source,_=history[(asset,choice['timeframe'])]
        start=next((i for i,b in enumerate(source) if b.timestamp>=start_time),len(source))
        end=next((i for i,b in enumerate(source) if b.timestamp>end_time),len(source))
        warm=max(0,start-200)
        bars=source[warm:end]
        params=choice['params']
        settings=StrategySettings(asset,42,entry_threshold=params['entry'],stop_loss_atr=params['stop'],take_profit_atr=params['stop']*2)
        strategy=AdaptiveStrategy(choice['family'],asset,choice['family'],features=causal_features(bars),params=params)
        session=TradingSession(BacktestEngine(initial_equity=initial/3,**cost_factory(asset)),strategy,settings,spec_factory(asset),warmup=start-warm)
        for bar in bars[:start-warm]: session.on_bar(bar)
        sessions[asset]=session
        for bar in bars[start-warm:]: heapq.heappush(heap,(bar.timestamp,asset,bar))
    policy=RiskPolicy()
    risk=RiskState(initial,initial)
    daily={}
    curve=[initial]
    maximum_gross=0.
    while heap:
        timestamp,asset,bar=heapq.heappop(heap)
        session=sessions[asset]
        session.on_bar(bar)
        total=idle+sum(s.value(s.bars[-1].close) if s.bars else initial/3 for s in sessions.values())
        gross=sum(s.gross(s.bars[-1].close) if s.bars else 0 for s in sessions.values())
        maximum_gross=max(maximum_gross,gross/max(total,1e-12))
        allowed=risk.assess(total,timestamp,policy) and gross<=total*policy.max_portfolio_exposure
        if not allowed:
            # 非同时开市的资产在各自下一可交易时点退出，不用陈旧报价伪造成交。
            for s in sessions.values():
                s.risk.halted=True
                s.risk.reason='组合风险预算触发'
                s.pending=[]
        daily[int(timestamp//86400)]=total
        curve.append(total)
    trades=[]
    liquidations=0
    final=idle
    for session in sessions.values():
        result=session.result()
        trades.extend(result.trades)
        final+=result.diagnostics['final_equity']
        liquidations+=result.diagnostics['liquidation_count']
    if daily: daily[max(daily)]=final
    daily_curve=[initial]
    if daily:
        for day in range(min(daily),max(daily)+1): daily_curve.append(daily.get(day,daily_curve[-1]))
    metrics=calculate_metrics(daily_curve,trades,periods_per_year=365)
    dd=_max_drawdown(curve+[final])
    metrics=replace(metrics,max_drawdown=dd,calmar_ratio=metrics.annualized_return/dd if dd else 0)
    return dict(initial_equity=initial,final_equity=final,total_return=final/initial-1,
        metrics=asdict(metrics),liquidation_count=liquidations,max_gross_leverage=maximum_gross,
        risk_halted=risk.halted,asset_budget=initial/3,selected=selections,
        daily_equity=daily,method='共同5000美元，每资产预留1/3；实际手数舍入及组合风控，未使用预算保持现金')
