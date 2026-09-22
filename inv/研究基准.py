"""现金、无杠杆买入持有和基于历史波动率的等风险被动基准。"""
from dataclasses import asdict,replace
from statistics import pstdev
from math import sqrt
from inv.backtest.metrics import calculate_metrics


def benchmarks(bars,start,end,spec,cost):
    initial=5000.
    output={}
    for name in ('cash','buy_hold','equal_risk'):
        cash=initial
        volume=0.
        equity=[initial]
        daily={}
        total_cost=turnover=0.
        for i in range(start,end):
            bar=bars[i]
            if name=='cash':
                target=0.
            elif name=='buy_hold':
                target=spec.normalize_volume(initial/(bar.open*spec.contract_size*(1+cost['commission_pct']+cost['slippage_pct']+cost['spread_pct']))) if i==start else volume
            else:
                tail=bars[max(0,i-21):i]
                returns=[b.close/a.close-1 for a,b in zip(tail,tail[1:])]
                # 使用上一根之前的波动率，目标年化15%，最高1倍名义敞口。
                interval=(tail[-1].timestamp-tail[0].timestamp)/max(1,len(tail)-1) if len(tail)>1 else 86400
                annual_vol=pstdev(returns)*sqrt(365*86400/interval) if len(returns)>1 else 0
                exposure=min(1,.15/annual_vol) if annual_vol else 0
                current=cash+volume*bar.open*spec.contract_size
                target=spec.normalize_volume(max(0,current)*exposure/(bar.open*spec.contract_size))
            delta=target-volume
            fee=abs(delta)*bar.open*spec.contract_size*(cost['commission_pct']+cost['slippage_pct']+cost['spread_pct']/2)
            cash-=delta*bar.open*spec.contract_size+fee
            total_cost+=fee
            turnover+=abs(delta)*bar.open*spec.contract_size
            if i>start:
                funding=volume*bar.open*spec.contract_size*cost['funding_daily']*(bar.timestamp-bars[i-1].timestamp)/86400
                cash-=funding
                total_cost+=funding
            volume=target
            value=cash+volume*bar.close*spec.contract_size
            equity.append(value)
            daily[int(bar.timestamp//86400)]=value
        if end>start:
            final_price=bars[end-1].close
            fee=volume*final_price*spec.contract_size*(cost['commission_pct']+cost['slippage_pct']+cost['spread_pct']/2)
            equity[-1]-=fee
            total_cost+=fee
            if daily: daily[max(daily)]=equity[-1]
        daily_curve=[initial]
        if daily:
            for day in range(min(daily),max(daily)+1): daily_curve.append(daily.get(day,daily_curve[-1]))
        m=calculate_metrics(daily_curve,[],periods_per_year=365)
        output[name]={**asdict(m),'net_profit':equity[-1]-initial,'total_return':equity[-1]/initial-1,
                      'final_equity':equity[-1],'total_costs':total_cost,'turnover_notional':turnover}
    return output
