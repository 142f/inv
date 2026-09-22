"""统一交易入口：默认无账户 Paper，实盘显式启用。"""
import argparse
from dataclasses import asdict
import json
import hashlib
import os
from pathlib import Path
import random
import time

from inv.backtest.engine import BacktestEngine
from inv.domain.models import Bar
from inv.domain.settings import StrategySettings
from inv.strategy.行情自适应 import AdaptiveStrategy, FAMILIES, causal_features
from inv.仓位风控 import RiskPolicy
from inv.交易会话 import TradingSession
from 策略研究 import public_spec, costs, summary, save, aligned_history,implementation_hash


def synthetic(count=500):
    rng=random.Random(42)
    price=100.
    for i in range(count):
        close=price*(1+rng.gauss(.0003,.01))
        yield Bar(1704067200+i*86400,price,max(price,close)*1.005,min(price,close)*.995,close)
        price=close


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['paper','backtest','live'],default='paper')
    parser.add_argument('--symbol',choices=['BTC','ETH','XAU'],default='BTC')
    parser.add_argument('--timeframe',choices=['D1','H4'],default='D1')
    parser.add_argument('--family',choices=FAMILIES,default='regime_adaptive')
    parser.add_argument('--config',default='config/公开运行配置.json')
    parser.add_argument('--history',default=r'E:\Project\inv-trend-trading\data')
    parser.add_argument('--real-history',action='store_true')
    parser.add_argument('--cycles',type=int,default=1)
    parser.add_argument('--interval',type=float,default=1)
    parser.add_argument('--max-seconds',type=float,default=0)
    parser.add_argument('--live-enabled',action='store_true')
    parser.add_argument('--checkpoint',help='Paper检查点路径，可从此前检查点恢复')
    parser.add_argument('--portfolio',action='store_true',help='使用锁定策略运行同一账户三资产组合')
    parser.add_argument('--selection-lock',default='reports/改造验证/参数锁定研究.json')
    parser.add_argument('--acceptance',default='reports/改造验证/策略验收结论.json')
    args=parser.parse_args(argv)
    if args.cycles<1 or args.interval<0 or args.max_seconds<0: parser.error('运行次数或间隔无效')
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    params=config['parameters']
    policy=RiskPolicy(**config['risk'])
    configuration=dict(config=config,asset=args.symbol,timeframe=args.timeframe,family=args.family,portfolio=args.portfolio,
                       real_history=args.real_history,implementation=implementation_hash())
    if args.portfolio:
        configuration['selection']=json.loads(Path(args.selection_lock).read_text(encoding='utf-8'))['selection']
    fingerprint=hashlib.sha256(json.dumps(configuration,sort_keys=True).encode()).hexdigest()
    broker_symbol=config['symbols'][args.symbol]
    settings=StrategySettings(broker_symbol,config['magic'],entry_threshold=params['entry'],
                              stop_loss_atr=params['stop'],take_profit_atr=params['stop']*2)
    if args.mode=='live':
        if not args.live_enabled: parser.error('Live 必须显式指定 --live-enabled')
        if args.family=='fixed_grid': parser.error('固定网格仅供研究比较，禁止实盘启用')
        decisions=json.loads(Path(args.acceptance).read_text(encoding='utf-8'))
        accepted={asset:row['research_candidate'] for asset,row in decisions.items()
                  if row.get('economic_pass') and row.get('provenance_pass')
                  and row.get('implementation_hash')==implementation_hash()}
        if args.portfolio:
            accepted={asset:choice for asset,choice in accepted.items()
                      if configuration['selection'].get(asset)==choice}
            if not accepted: parser.error('没有同时通过封存经济与来源验收的组合候选')
        else:
            choice=accepted.get(args.symbol)
            if not choice or choice['family']!=args.family or choice['timeframe']!=args.timeframe:
                parser.error('该资产／策略／周期未通过封存经济与来源验收，保持Paper')
            params=choice['params']
            settings=StrategySettings(broker_symbol,config['magic'],entry_threshold=params['entry'],
                                      stop_loss_atr=params['stop'],take_profit_atr=params['stop']*2)
        configuration['accepted_selection']=accepted
        fingerprint=hashlib.sha256(json.dumps(configuration,sort_keys=True).encode()).hexdigest()
        # 仅实盘分支加载 MT5；不调用旧 Security，不读取 .env 或策略密钥。
        import MetaTrader5 as mt5
        from inv.执行状态库 import ExecutionStateStore
        from inv.实盘适配 import MT5Adapter,LiveRunner
        names=('MT5_LOGIN','MT5_PASSWORD','MT5_SERVER')
        if not all(os.environ.get(name) for name in names): parser.error('需要 MT5_LOGIN、MT5_PASSWORD、MT5_SERVER 环境变量')
        if not mt5.initialize(login=int(os.environ['MT5_LOGIN']),password=os.environ['MT5_PASSWORD'],server=os.environ['MT5_SERVER']):
            raise RuntimeError('MT5 登录失败；不输出账号或凭证')
        store=None
        try:
            if not mt5.symbol_select(broker_symbol,True): raise RuntimeError('品种不可用，请核对公开品种映射')
            identity=hashlib.sha256((os.environ['MT5_LOGIN']+'@'+os.environ['MT5_SERVER']).encode()).hexdigest()[:16]
            store=ExecutionStateStore(Path('运行状态')/f'账户_{identity}_执行状态.json',exclusive=True)
            prior=store.get('configuration_hash')
            if prior is not None and prior!=fingerprint:
                raise RuntimeError('运行配置与持久化状态不一致，请先核对已有持仓与状态后再迁移配置')
            store.set('configuration_hash',fingerprint)
            store.flush()
            adapter=MT5Adapter(mt5,store,enabled=True)
            runners=[]
            if args.portfolio:
                locked=json.loads(Path(args.selection_lock).read_text(encoding='utf-8'))
                for asset,choice in accepted.items():
                    if not choice: continue
                    name=config['symbols'][asset]
                    if not mt5.symbol_select(name,True): raise RuntimeError('组合品种不可用')
                    parameters=choice['params']
                    asset_settings=StrategySettings(name,config['magic'],entry_threshold=parameters['entry'],
                        stop_loss_atr=parameters['stop'],take_profit_atr=parameters['stop']*2)
                    strategy=AdaptiveStrategy(choice['family'],name,choice['family'],params=parameters)
                    runners.append(LiveRunner(mt5,adapter,strategy,asset_settings,timeframe=choice['timeframe'],policy=policy,allocation=1/3))
                if not runners: raise RuntimeError('锁定研究没有合格候选，组合保持现金')
            else:
                strategy=AdaptiveStrategy(args.family,broker_symbol,args.family,params=params)
                runners=[LiveRunner(mt5,adapter,strategy,settings,timeframe=args.timeframe,policy=policy)]
            start=time.monotonic()
            failures=0
            for i in range(args.cycles):
                if args.max_seconds and time.monotonic()-start>=args.max_seconds: break
                try:
                    for runner in runners:
                        print(json.dumps(runner.cycle(),ensure_ascii=False))
                    failures=0
                except (RuntimeError,ConnectionError) as exc:
                    failures+=1
                    print(f'运行暂停：{exc}')
                    if failures>=3 or i+1==args.cycles: raise
                    # 重连后仍须恢复状态并对账；未知订单不会因重连重发。
                    if mt5.account_info() is None:
                        mt5.shutdown()
                        if not mt5.initialize(login=int(os.environ['MT5_LOGIN']),password=os.environ['MT5_PASSWORD'],server=os.environ['MT5_SERVER']):
                            continue
                if i+1<args.cycles: time.sleep(args.interval)
        finally:
            if store is not None: store.close()
            mt5.shutdown()
        return 0
    if args.portfolio:
        from inv.组合验证 import portfolio_replay
        locked=json.loads(Path(args.selection_lock).read_text(encoding='utf-8'))
        history=aligned_history(args.history)
        start=max(d['start'] for d in locked['datasets'].values())
        end=min(d['seal_time'] for d in locked['datasets'].values())-1
        result=portfolio_replay(history,locked['selection'],start_time=start,end_time=end,spec_factory=public_spec,cost_factory=costs)
        save('组合运行验证.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='daily_equity'},ensure_ascii=False))
        return 0
    if args.real_history or args.mode=='backtest':
        bars,_=aligned_history(args.history)[(args.symbol,args.timeframe)]
        bars=bars[:int(len(bars)*.8)]
        source='真实历史开发段，末20%未访问策略计算'
    else:
        bars=list(synthetic())
        source='固定种子合成行情，仅验证工程行为'
    features=causal_features(bars)
    strategy=AdaptiveStrategy(args.family,broker_symbol,args.family,features=features,params=params)
    engine=BacktestEngine(initial_equity=5000,risk_policy=policy,**costs(args.symbol))
    if args.mode=='paper':
        checkpoint=Path(args.checkpoint) if args.checkpoint else None
        if checkpoint and checkpoint.exists():
            payload=json.loads(checkpoint.read_text(encoding='utf-8'))
            if payload.get('configuration_hash')!=fingerprint:
                raise ValueError('Paper检查点配置不匹配，不能直接复用旧状态')
            session=TradingSession.restore(payload,engine,strategy,settings,public_spec(args.symbol))
        else:
            session=TradingSession(engine,strategy,settings,public_spec(args.symbol))
        for bar in bars:
            if session.last_time is None or bar.timestamp>session.last_time: session.on_bar(bar)
        if checkpoint:
            from inv.执行状态库 import ExecutionStateStore
            store=ExecutionStateStore(checkpoint)
            store.values={**session.checkpoint(),'configuration_hash':fingerprint}
            store.flush()
        result=session.result()
    else:
        result=engine.run(strategy,bars,settings,public_spec(args.symbol))
    payload=dict(mode=args.mode,source=source,asset=args.symbol,timeframe=args.timeframe,family=args.family,metrics=summary(result))
    save('运行验证.json',payload)
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
