"""从结构化验证结果生成最终中文报告，不计算或调整策略参数。"""
import json
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path('reports/改造验证')


def load(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))


def pct(value):
    return '不可定义' if value is None else f'{value:.2%}'


def num(value):
    return '不可定义' if value is None else f'{value:.3f}'


def date(value):
    return datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%d')


def main():
    study=load('参数锁定研究.json')
    final=load('封存验收.json')
    extra=load('研究补充验证.json')
    pressure=load('压力测试.json')
    tests=load('测试结果.json')
    lines=['# 策略回测与压力测试报告','',
        '## 验收结论','',
        '三资产真实行情已完成开发研究及一次性末20%封存验收。研究候选按开发段锁定；封存结果仅用于验收，没有重新调参。候选失败时保留不交易结论。',
        '',f'工程测试：{tests["tests_run"]}项，失败{tests["failures"]}项，错误{tests["errors"]}项。',
        '', '## 数据与实验口径','',
        '| 资产/周期 | 开发开始 | 封存开始 | 结束 | 对齐后根数 | 来源状态 |',
        '|---|---|---|---|---:|---|']
    for key,d in study['datasets'].items():
        lines.append(f'| {key} | {date(d["start"])} | {date(d["seal_time"])} | {date(d["end"])} | {d["aligned_rows"]} | {", ".join(d["quality_status"])} |')
    lines+=['','5m、15m、30m、1H缺少本次可确认数据，未从高周期反推。H4及XAU数据带LEGACY_ONLY、RAW_SOURCE_UNAVAILABLE、NO_PROVIDER_VERIFICATION等标记；其结果为探索性证据，不能作为已通过真实经纪商验证的证明。',
        '', '每个单资产实验初始5000美元；实际组合总共5000美元。最大杠杆10倍，风险筛选回撤25%。所有资产独立选策略、周期和参数。',
        '', '先取前80%开发段，其中3个连续前推验证窗，训练区间逐步扩展。每组策略固定种子42抽取4组候选，检验±10%邻域及3倍点差／滑点压力。4组属于有限预算探索，不宣称穷尽参数空间或找到全局最优。验证窗每窗至少10笔交易，零强平、回撤不超过25%；按Calmar中位数、Sharpe中位数及低换手排序，邻域不能仅有孤立最优点。',
        '', '成本均为公开保守研究假设：单边佣金5bps、滑点5bps；BTC/ETH点差5bps、每日融资5bps；XAU点差2bps、每日隔夜成本2bps。缺少实际费率时不冒充经纪商报价。XAU以100盎司/手、加密资产1币/手模拟，手数最小与步长0.01；Live读取实际券商规格。',
        '', '研究日历按UTC计算日收益年化365；收盘信号下一开盘执行，歧义限价撮合采用保守规则。Paper与回测共用账本；网格库存受层级预算、最小手数、资产／组合敞口及保证金约束，不无限加仓。',
        '', '## 锁定候选与封存结果','',
        '| 资产 | 开发段锁定候选 | 封存收益 | 最大回撤 | Sharpe | 交易数 | 判定 |',
        '|---|---|---:|---:|---:|---:|---|']
    decisions={}
    for asset,choice in study['selection'].items():
        if not choice:
            lines.append(f'| {asset} | 无合格候选，保持现金 | — | — | — | 0 | 不交易 |')
            decisions[asset]=dict(enabled=False,reason='开发段无合格候选',implementation_hash=study['implementation_hash'])
            continue
        row=next(r for r in final['cases'] if r['asset']==asset and r['timeframe']==choice['timeframe'] and r['family']==choice['family'])
        m=row['metrics']
        economic=m['total_return']>0 and m['max_drawdown']<=.25 and m['liquidation_count']==0 and m['trade_count']>=10
        provenance=study['datasets'][f'{asset}/{choice["timeframe"]}']['quality_status']==['CURATED']
        verdict='经济筛选通过，账号验证待完成' if economic and provenance else ('探索性通过，来源未核验' if economic else '封存验收失败，不启用')
        lines.append(f'| {asset} | {choice["timeframe"]} / {choice["family"]} | {pct(m["total_return"])} | {pct(m["max_drawdown"])} | {num(m["sharpe_ratio"])} | {m["trade_count"]} | {verdict} |')
        decisions[asset]=dict(enabled=False,research_candidate=choice,economic_pass=economic,provenance_pass=provenance,
                             reason=verdict,implementation_hash=study['implementation_hash'])
    lines+=['','所有资产默认仍为Paper；研究通过不等于已获实盘账号成交验证。策略候选参数：','']
    for asset,choice in study['selection'].items():
        if choice:
            p=choice['params']
            lines.append(f'- {asset}：{choice["timeframe"]}突破；前20根高低通道，收盘突破后下一可交易时点开仓，ATR14止损{p["stop"]}倍、止盈{p["stop"]*2}倍，移动止损3ATR。恐慌或异常波动停止开仓；回到通道／趋势反向时退出。网格spacing/levels字段为统一实验参数，突破组不使用，不应误读成有效突破参数。')
        else:
            lines.append(f'- {asset}：保持现金；不为了覆盖三资产而强行选出亏损策略。')
    lines+=['','仓位风险金额＝权益×1%×信号强度×回撤缩减，再除以止损距离及盈亏乘数，并按最小手数向下取整。网格还除以全部网格层数分享风险。信号强度为ADX、均线斜率及标准化动量的0～1组合；更强信号可以增加仓位，但仍受保证金和敞口上限限制。',
        '', '网格研究规则：SMA20中心（固定网格在首次决策冻结中心），spacing×ATR14，候选层数3/5/7/10/15/20；ADX低于候选阈值、均线斜率绝对值<0.5、ATR分位在[0.1,0.95)且非突破时允许自适应网格；高波动减少层数，恐慌、趋势或库存／保证金风险触发时关闭。最终锁定候选未采用网格，不能把研究网格参数作为实盘推荐。',
        '', '## 全部封存比较','',
        '| 资产 | 周期 | 策略 | 收益 | CAGR | Sharpe | Sortino | 最大回撤 | Calmar | 交易 | 强平 |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in final['cases']:
        m=r['metrics']
        lines.append(f'| {r["asset"]} | {r["timeframe"]} | {r["family"]} | {pct(m["total_return"])} | {pct(m["annualized_return"])} | {num(m["sharpe_ratio"])} | {num(m["sortino_ratio"])} | {pct(m["max_drawdown"])} | {num(m["calmar_ratio"])} | {m["trade_count"]} | {m["liquidation_count"]} |')
    lines+=['','完整Profit Factor、胜率、Expectancy、平均交易、频率、持仓时间、敞口、最高杠杆、保证金和费用见`封存验收.json`。零交易和无法定义的比率不作为获利证据。此表用于完整报告，禁止看表后把另一组改成新的封存赢家。',
        '', '## 5000美元组合','']
    p=final['portfolio']
    lines.append(f'锁定候选组合：5000 → {p["final_equity"]:.2f}美元，总收益{pct(p["total_return"])}，最大回撤{pct(p["metrics"]["max_drawdown"])}，Sharpe {num(p["metrics"]["sharpe_ratio"])}，强平{p["liquidation_count"]}次。')
    lines+=['','每资产预留1666.67美元、未选择资产保持现金；实际按手数步长模拟，组合风险可停止所有新增订单。以上是预先锁定候选的验收结果，含来源尚未核验的BTC H4，不代表已经可实盘推荐的组合。',
        '', '## 基准与收益来源','',
        '| 资产/周期 | 现金收益 | 买入持有收益 | 等风险持有收益 |',
        '|---|---:|---:|---:|']
    for key,group in final['benchmarks'].items():
        lines.append(f'| {key} | {pct(group["cash"]["total_return"])} | {pct(group["buy_hold"]["total_return"])} | {pct(group["equal_risk"]["total_return"])} |')
    lines+=['','买入持有最高1倍；等风险基准使用此前20期收益估计波动率、目标年化15%、最高1倍。均扣除同口径交易与持有成本，不能把基准上涨当作策略Alpha。',
        '', '| 开发段消融 | 资产 | 收益 | 费用 | 最高实际杠杆 |','|---|---|---:|---:|---:|']
    for a in extra['ablations']:
        m=a['metrics']
        lines.append(f'| {a["case"]} | {a["asset"]} | {pct(m["total_return"])} | {num(m["total_costs"])} | {num(m["max_leverage"])} |')
    lines+=['','各策略多空PnL、前五笔交易贡献和策略分项PnL保存在JSON。零成本消融仅解释成本侵蚀，不作为可执行收益。',
        '', 'BTC/ETH开发段收益相关性：D1约0.778，H4约0.801，不能视为独立分散。ETH/BTC过滤器追加消融：','']
    for row in extra.get('eth_btc_filter',[]):
        lines.append(f'- BTC过滤器={row["btc_filter"]}：三个开发验证窗Calmar中位数{num(row["median_calmar"])}。')
    lines+=['','过滤器没有改变锁定候选，追加实验需新的独立样本确认。XAU美元／美债宏观特征与经纪商交易日历缺少数据，未虚构验证。',
        '', '## 压力测试','',
        '| 资产 | 场景 | 账户收益 | 最大回撤 | 强平次数 |','|---|---|---:|---:|---:|']
    for row in pressure['tests']:
        m=row['metrics']
        lines.append(f'| {row["asset"]} | {row["scenario"]} | {pct(m["total_return"])} | {pct(m["max_drawdown"])} | {m["liquidation_count"]} |')
    lines+=['','压力场景先建立满信号风险预算持仓，再施加冲击。BTC/ETH跳空50%时账户亏损约25.11%，说明25%风控阈值不是止损成交保证。默认预算场景没有强平；另有高敞口故障注入测试确认强平计数和停止交易路径确实生效，不能据此声称任何行情都不会爆仓。',
        '', '## 可复现证据','',f'- 研究代码哈希：`{study["implementation_hash"]}`。',
        f'- 参数协议哈希：`{study["protocol_hash"]}`。',
        '- 开发结果：`参数锁定研究.json`；最终结果：`封存验收.json`；补充消融：`研究补充验证.json`。',
        '- 12次迭代评分及扣分原因见《阶段迭代与评分记录》；运行命令与实盘待验证项见《运行与实盘接入说明》。']
    (ROOT/'策略回测与压力测试报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (ROOT/'策略验收结论.json').write_text(json.dumps(decisions,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(decisions=decisions,portfolio_final=p['final_equity']),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
