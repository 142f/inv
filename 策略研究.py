"""三资产七策略的封存样本研究；开发与最终验收显式分离。"""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
from statistics import median

from inv.backtest.engine import BacktestEngine
from inv.backtest.optimizer import ParameterGrid
from inv.domain.models import SymbolSpec
from inv.domain.settings import StrategySettings
from inv.strategy.行情自适应 import AdaptiveStrategy, FAMILIES, causal_features
from inv.交易会话 import TradingSession
from inv.历史行情 import load_history

OUTPUT=Path('reports/改造验证')


def implementation_hash():
    """锁定影响策略结果的源文件，防止代码变更后误用旧参数验收。"""
    paths=[]
    for folder in ('inv/backtest','inv/domain','inv/strategy'):
        paths.extend(Path(folder).glob('*.py'))
    paths.extend(Path(p) for p in ('inv/交易会话.py','inv/仓位风控.py','inv/组合验证.py',
                                   'inv/研究基准.py','inv/历史行情.py','策略研究.py'))
    digest=hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def public_spec(asset):
    contract=100 if asset=='XAU' else 1
    return SymbolSpec(asset,.01,.01,10000,.01,contract_size=contract,tick_size=.01,tick_value=.01*contract)


def costs(asset, stress=1):
    # 非经纪商报价：统一保守研究假设，所有模式使用相同口径。
    return dict(commission_pct=.0005,slippage_pct=.0005*stress,
                spread_pct=(.0002 if asset=='XAU' else .0005)*stress,
                funding_daily=.0002 if asset=='XAU' else .0005,trailing_atr=3)


def run_case(asset,family,bars,features,params,start,end,stress=1):
    warm_start=max(0,start-200)
    settings=StrategySettings(asset,42,lookback_period=20,entry_threshold=params['entry'],
                              stop_loss_atr=params['stop'],take_profit_atr=params['stop']*2)
    strategy=AdaptiveStrategy(family,asset,family,features=features[warm_start:end],params=params)
    session=TradingSession(BacktestEngine(**costs(asset,stress)),strategy,settings,public_spec(asset),warmup=start-warm_start)
    for bar in bars[warm_start:end]: session.on_bar(bar)
    return session.result()


def summary(result):
    d=result.diagnostics
    metrics=asdict(result.metrics)
    metrics.update({k:d[k] for k in ('final_equity','total_costs','funding','spread_cost','max_leverage',
        'max_margin_usage','liquidation_count','exposure','risk_halted','risk_reason','average_holding_seconds')})
    metrics['total_return']=d['final_equity']/5000-1
    metrics['expectancy']=sum(t.net_pnl for t in result.trades)/max(1,len(result.trades))
    metrics['average_trade']=metrics['expectancy']
    metrics['recovery_factor']=metrics['total_return']/max(metrics['max_drawdown'],1e-12)
    times=d['timestamps']
    years=(times[-1]-times[0])/86400/365 if len(times)>1 else 0
    metrics['trades_per_year']=len(result.trades)/years if years else 0
    metrics['long_pnl']=sum(t.net_pnl for t in result.trades if t.side=='long')
    metrics['short_pnl']=sum(t.net_pnl for t in result.trades if t.side=='short')
    metrics['top_five_trade_pnl']=sum(sorted((t.net_pnl for t in result.trades),reverse=True)[:5])
    by_strategy={}
    for row in d['attribution']:
        by_strategy[row['strategy']]=by_strategy.get(row['strategy'],0)+row['net_pnl']
    metrics['strategy_pnl']=by_strategy
    return metrics


def rank(metrics):
    if metrics['liquidation_count'] or metrics['max_drawdown']>.25 or metrics['trade_count']<10:
        return (-1e9,-1e9,-1e9)
    return (metrics['calmar_ratio'],metrics['sharpe_ratio'],-metrics['turnover'])


def clean(value):
    import math
    if isinstance(value,float) and not math.isfinite(value): return None
    if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    return value


def save(name,payload):
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/name).write_text(json.dumps(clean(payload),ensure_ascii=False,indent=2),encoding='utf-8')


def aligned_history(root):
    history=load_history(root)
    for tf in ('D1','H4'):
        group=[history[(asset,tf)][0] for asset in ('BTC','ETH','XAU') if (asset,tf) in history]
        if not group: continue
        start=max(b[0].timestamp for b in group)
        end=min(b[-1].timestamp for b in group)
        for asset in ('BTC','ETH','XAU'):
            if (asset,tf) not in history: continue
            bars,meta=history[(asset,tf)]
            history[(asset,tf)]=([b for b in bars if start<=b.timestamp<=end],meta)
    return history


def develop(args):
    history=aligned_history(args.history)
    candidates=ParameterGrid(dict(spacing=[.3,.5,.8,1,1.5,2],levels=[3,5,7,10,15,20],
                                 entry=[1.5,2],stop=[2,3],trend_adx=[20,25,30])).sample(args.candidates,42)
    result=dict(seed=42,initial_equity=5000,max_leverage=10,drawdown_target=.25,implementation_hash=implementation_hash(),
                phase='development',candidates=candidates,datasets={},cases=[],missing_timeframes=['5m','15m','30m','1H'],
                cost_assumptions={a:costs(a) for a in ('BTC','ETH','XAU')})
    for (asset,tf),(bars,meta) in history.items():
        seal=int(len(bars)*.8)
        # 封存段不送入特征计算；元数据仅记录其边界。
        development=bars[:seal]
        features=causal_features(development)
        result['datasets'][f'{asset}/{tf}']={**meta,'aligned_rows':len(bars),'seal_index':seal,
            'start':bars[0].timestamp,'end':bars[-1].timestamp,'seal_time':bars[seal].timestamp}
        from inv.研究基准 import benchmarks
        result['datasets'][f'{asset}/{tf}']['benchmarks']=benchmarks(development,0,len(development),public_spec(asset),costs(asset))
        n=len(development)
        windows=[(int(n*(.5+i/6)),int(n*(.5+(i+1)/6))) for i in range(3)]
        for family in FAMILIES:
            trials=[]
            for params in candidates:
                folds=[]
                for start,end in windows:
                    trained=summary(run_case(asset,family,development,features,params,0,start))
                    validation=summary(run_case(asset,family,development,features,params,start,end))
                    folds.append(dict(train_end=start,validation_end=end,training=trained,validation=validation))
                keys=[rank(f['validation']) for f in folds]
                eligible=all(k[0]>-1e8 for k in keys)
                score=tuple(median(k[i] for k in keys) for i in range(3)) if eligible else (-1e9,)*3
                trials.append(dict(params=params,folds=folds,rank=score,eligible=eligible))
            best=max(trials,key=lambda row:row['rank'])
            case=dict(asset=asset,timeframe=tf,family=family,selected=best['params'],rank=best['rank'],
                      eligible=best['eligible'],trials=trials)
            case['development']=summary(run_case(asset,family,development,features,best['params'],0,n))
            if args.sensitivity:
                neighbors=[]
                for factor in (.9,1.1):
                    parameters={**best['params'], 'spacing':best['params']['spacing']*factor,
                                'entry':best['params']['entry']*factor,'stop':best['params']['stop']*factor}
                    samples=[summary(run_case(asset,family,development,features,parameters,a,b)) for a,b in windows]
                    neighbors.append(dict(factor=factor,metrics=samples,
                        calmar=median(m['calmar_ratio'] for m in samples),eligible=all(rank(m)[0]>-1e8 for m in samples)))
                case['sensitivity']=neighbors
                case['cost_stress']=summary(run_case(asset,family,development,features,best['params'],windows[-1][0],n,3))
                case['eligible']=case['eligible'] and all(r['eligible'] and r['calmar']>0 for r in neighbors)
            result['cases'].append(case)
            print(f'{asset}/{tf} {family}: validated={best["eligible"]}, trades={case["development"]["trade_count"]}',flush=True)
            save(args.output,result)
    # 不采用全开发段收益重新选择策略。
    result['selection']={}
    for asset in ('BTC','ETH','XAU'):
        eligible=[c for c in result['cases'] if c['asset']==asset and c['eligible']]
        best=max(eligible,key=lambda row:row['rank']) if eligible else None
        result['selection'][asset]=dict(timeframe=best['timeframe'],family=best['family'],params=best['selected']) if best and best['rank'][0]>0 else None
    result['protocol_hash']=hashlib.sha256(json.dumps(candidates,sort_keys=True).encode()).hexdigest()
    save(args.output,result)


def finalize(args):
    development=json.loads((OUTPUT/args.lock).read_text(encoding='utf-8'))
    if development.get('implementation_hash')!=implementation_hash():
        raise ValueError('研究代码发生变化，必须先重新完成开发段验证')
    final_path=OUTPUT/args.output
    if final_path.exists():
        raise ValueError('最终封存样本已验收，禁止覆盖或再次调参；原结果应保留')
    history=aligned_history(args.history)
    rows=[]
    curves={}
    for case in development['cases']:
        asset,tf=case['asset'],case['timeframe']
        bars,meta=history[(asset,tf)]
        locked=development['datasets'][f'{asset}/{tf}']
        if meta['sha256']!=locked['sha256']: raise ValueError('封存数据发生变化')
        start=locked['seal_index']
        features=causal_features(bars)
        result=run_case(asset,case['family'],bars,features,case['selected'],start,len(bars))
        row=dict(asset=asset,timeframe=tf,family=case['family'],params=case['selected'],metrics=summary(result),
                 development_eligible=case['eligible'])
        rows.append(row)
        selection=development['selection'].get(asset)
        if selection and selection['family']==case['family'] and selection['timeframe']==tf:
            curves[asset]=result.diagnostics['daily_equity']
    payload=dict(phase='sealed_acceptance',development_file=args.lock,protocol_hash=development['protocol_hash'],
                 selection=development['selection'],cases=rows)
    from inv.组合验证 import portfolio_replay
    from inv.研究基准 import benchmarks
    payload['benchmarks']={}
    for key,locked in development['datasets'].items():
        asset,tf=key.split('/')
        bars,_=history[(asset,tf)]
        payload['benchmarks'][key]=benchmarks(bars,locked['seal_index'],len(bars),public_spec(asset),costs(asset))
    start=max(d['seal_time'] for d in development['datasets'].values())
    end=min(d['end'] for d in development['datasets'].values())
    payload['portfolio']=portfolio_replay(history,development['selection'],start_time=start,end_time=end,
                                         spec_factory=public_spec,cost_factory=costs)
    save(args.output,payload)
    print('封存验收完成，结果已锁定',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history',default=r'E:\Project\inv-trend-trading\data')
    parser.add_argument('--phase',choices=['development','finalize'],default='development')
    parser.add_argument('--candidates',type=int,default=4)
    parser.add_argument('--sensitivity',action='store_true')
    parser.add_argument('--output',default='开发研究.json')
    parser.add_argument('--lock',default='开发研究.json')
    args=parser.parse_args()
    if not 1<=args.candidates<=100: parser.error('候选数量必须在1到100之间')
    (develop if args.phase=='development' else finalize)(args)


if __name__=='__main__': main()
