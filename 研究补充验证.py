"""开发段组合、成本归因和资产相关性验证，不读取封存段生成策略结果。"""
import argparse
import json
from dataclasses import replace
from statistics import median
import pandas as pd
from 策略研究 import aligned_history,OUTPUT,save,costs,public_spec,run_case,summary
from inv.组合验证 import portfolio_replay
from inv.strategy.行情自适应 import causal_features
from inv.交易会话 import TradingSession
from inv.backtest.engine import BacktestEngine
from inv.domain.settings import StrategySettings
from inv.strategy.行情自适应 import AdaptiveStrategy
from inv.仓位风控 import RiskPolicy
from inv.domain.models import SignalType


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock',default='第二轮研究.json')
    parser.add_argument('--history',default=r'E:\Project\inv-trend-trading\data')
    args=parser.parse_args()
    d=json.loads((OUTPUT/args.lock).read_text(encoding='utf-8'))
    history=aligned_history(args.history)
    starts=[]
    ends=[]
    for key,(bars,meta) in list(history.items()):
        seal=d['datasets']['/'.join(key)]['seal_index']
        history[key]=(bars[:seal],meta)
        starts.append(bars[0].timestamp)
        ends.append(bars[seal-1].timestamp)
    portfolio=portfolio_replay(history,d['selection'],start_time=max(starts),end_time=min(ends),
                               spec_factory=public_spec,cost_factory=costs)
    correlations={}
    for tf in ('D1','H4'):
        pairs={}
        for asset in ('BTC','ETH'):
            bars,_=history[(asset,tf)]
            pairs[asset]=pd.Series({b.timestamp:b.close for b in bars}).pct_change()
        frame=pd.DataFrame(pairs).dropna()
        correlations[tf]=dict(samples=len(frame),return_correlation=float(frame.BTC.corr(frame.ETH)),
            interpretation='仅开发段同期收益相关性；不据此假设独立分散，组合采用固定预算和总敞口上限')
    ablations=[]
    for asset,choice in d['selection'].items():
        if not choice: continue
        bars,_=history[(asset,choice['timeframe'])]
        features=causal_features(bars)
        p=choice['params']
        settings=StrategySettings(asset,42,entry_threshold=p['entry'],stop_loss_atr=p['stop'],take_profit_atr=p['stop']*2)
        for label,cost,policy in [('标准成本',costs(asset),RiskPolicy()),
                                  ('零成本归因',{**costs(asset),'commission_pct':0,'slippage_pct':0,'spread_pct':0,'funding_daily':0},RiskPolicy()),
                                  ('最高1倍',costs(asset),RiskPolicy(max_leverage=1,max_margin_usage=1,max_asset_exposure=1,max_portfolio_exposure=1))]:
            strategy=AdaptiveStrategy(choice['family'],asset,choice['family'],features=features,params=p)
            session=TradingSession(BacktestEngine(**cost,risk_policy=policy),strategy,settings,public_spec(asset))
            for bar in bars: session.on_bar(bar)
            ablations.append(dict(asset=asset,case=label,metrics=summary(session.result())))
    filter_trials=[]
    choice=d['selection'].get('ETH')
    if choice:
        import bisect
        tf=choice['timeframe']
        eth,_=history[('ETH',tf)]
        btc,_=history[('BTC',tf)]
        btc_features=causal_features(btc)
        btc_times=[b.timestamp for b in btc]
        p=choice['params']
        eth_features=causal_features(eth)
        class Filtered(AdaptiveStrategy):
            def compute_signals(self,bars,current_index,settings):
                signals=super().compute_signals(bars,current_index,settings)
                index=bisect.bisect_right(btc_times,bars[current_index].timestamp)-1
                if index<100:
                    return [s for s in signals if s.signal_type not in (SignalType.BUY,SignalType.SELL)]
                direction=btc_features[index]['momentum']
                return [s for s in signals if s.signal_type not in (SignalType.BUY,SignalType.SELL)
                        or (s.signal_type==SignalType.BUY and direction>0)
                        or (s.signal_type==SignalType.SELL and direction<0)]
        n=len(eth)
        for filtered in (False,True):
            folds=[]
            for j in range(3):
                start,end=int(n*(.5+j/6)),int(n*(.5+(j+1)/6))
                warm=max(0,start-200)
                kind=Filtered if filtered else AdaptiveStrategy
                strategy=kind(choice['family'],'ETH',choice['family'],features=eth_features[warm:end],params=p)
                settings=StrategySettings('ETH',42,entry_threshold=p['entry'],stop_loss_atr=p['stop'],take_profit_atr=p['stop']*2)
                session=TradingSession(BacktestEngine(**costs('ETH')),strategy,settings,public_spec('ETH'),warmup=start-warm)
                for bar in eth[warm:end]: session.on_bar(bar)
                folds.append(summary(session.result()))
            filter_trials.append(dict(btc_filter=filtered,folds=folds,median_calmar=median(f['calmar_ratio'] for f in folds)))
    save('研究补充验证.json',dict(development_only=True,portfolio=portfolio,correlation=correlations,ablations=ablations,
        eth_btc_filter=filter_trials,filter_decision='过滤器为锁定后开发段消融，不凭一次追加实验改变预注册候选；需独立后续样本才考虑上线'))
    print(json.dumps(dict(portfolio_final=portfolio['final_equity'],correlation=correlations),ensure_ascii=False))


if __name__=='__main__': main()
