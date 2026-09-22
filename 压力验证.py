"""在固定种子行情及已持仓条件下验证极端冲击，不用无持仓结果冒充压力通过。"""
from dataclasses import replace
import json
from inv.backtest.engine import BacktestEngine
from inv.domain.models import Bar,Signal,SignalType
from inv.domain.settings import StrategySettings
from inv.strategy.base import BaseStrategy
from inv.交易会话 import TradingSession
from 策略研究 import public_spec,costs,summary,save


class StressPosition(BaseStrategy):
    def __init__(self,asset,side):
        super().__init__('压力持仓',asset)
        self.side=side

    def compute_signals(self,bars,current_index,settings):
        if current_index==0:
            price=bars[0].close
            return [Signal(self._get_timestamp(bars,0),self.symbol,
                SignalType.BUY if self.side==1 else SignalType.SELL,stop_loss=price*(1-self.side*.02),confidence=1)]
        return []


def main():
    scenarios={'BTC':[-.2,-.3,-.5],'ETH':[-.3,-.5],'XAU':[-.15,.15]}
    rows=[]
    for asset,shocks in scenarios.items():
        price={'BTC':50000.,'ETH':2500.,'XAU':2000.}[asset]
        for shock in shocks:
            side=-1 if shock>0 else 1
            bars=[Bar(1704067200+i*86400,price,price,price,price) for i in range(2)]
            moved=price*(1+shock)
            bars.extend(Bar(1704067200+i*86400,moved,moved,moved,moved) for i in range(2,10))
            engine=BacktestEngine(**costs(asset))
            session=TradingSession(engine,StressPosition(asset,side),StrategySettings(asset,42),public_spec(asset))
            for bar in bars: session.on_bar(bar)
            result=session.result()
            assert result.metrics.trade_count>0,'压力测试必须先建立真实模拟持仓'
            assert not session.positions
            rows.append(dict(asset=asset,scenario=f'跳空{shock:.0%}',metrics=summary(result),passed=not session.positions))
        for scenario in ('波动3倍','点差3倍','滑点3倍'):
            cost=costs(asset)
            if scenario=='点差3倍': cost['spread_pct']*=3
            if scenario=='滑点3倍': cost['slippage_pct']*=3
            bars=[Bar(1704067200+i*86400,price,price,price,price) for i in range(2)]
            bars.append(Bar(1704067200+2*86400,price,price*1.06,price*.94,price))
            session=TradingSession(BacktestEngine(**cost),StressPosition(asset,1),StrategySettings(asset,42),public_spec(asset))
            for bar in bars: session.on_bar(bar)
            result=session.result()
            rows.append(dict(asset=asset,scenario=scenario,metrics=summary(result),passed=result.metrics.trade_count>0))
    save('压力测试.json',dict(seed=42,tests=rows,
        note='这是满信号风险预算持仓的故障场景，不等同于历史策略的最坏损失保证；三倍成本另有开发样本验证'))
    print(json.dumps([dict(asset=r['asset'],scenario=r['scenario'],return_pct=r['metrics']['total_return'],liquidations=r['metrics']['liquidation_count']) for r in rows],ensure_ascii=False,indent=2))


if __name__=='__main__': main()
