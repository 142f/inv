"""九类行情识别及统一策略选择；所有特征仅依赖当前及过去收盘。"""
from dataclasses import replace
import numpy as np
import pandas as pd
from inv.strategy.base import BaseStrategy
from inv.domain.models import Signal, SignalType


FAMILIES = ('fixed_grid', 'adaptive_grid', 'trend', 'breakout', 'mean_reversion', 'trend_grid', 'regime_adaptive')


def causal_features(bars, lookback=20):
    """向量化预计算因果指标；无中心窗口、负向位移或未来填充。"""
    frame = pd.DataFrame([(b.open, b.high, b.low, b.close, b.volume) for b in bars],
                         columns=['open', 'high', 'low', 'close', 'volume'])
    h, l, c = frame.high, frame.low, frame.close
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    up, down = h.diff(), -l.diff()
    plus = up.where((up > down) & (up > 0), 0).rolling(14).mean()
    minus = down.where((down > up) & (down > 0), 0).rolling(14).mean()
    dx = 100*(plus-minus).abs()/(plus+minus).replace(0, np.nan)
    mean = c.rolling(lookback).mean()
    std = c.rolling(lookback).std()
    result = pd.DataFrame(dict(
        center=mean, atr=atr, adx=dx.rolling(14).mean().fillna(0),
        slope=(mean-mean.shift(5))/atr,
        z=(c-mean)/std.replace(0,np.nan),
        upper=h.shift(1).rolling(lookback).max(), lower=l.shift(1).rolling(lookback).min(),
        atr_rank=atr.rolling(100, min_periods=30).rank(pct=True),
        momentum=(c/c.shift(lookback)-1)/c.pct_change().rolling(lookback).std().replace(0,np.nan),
        width=4*std/mean, volume_z=(frame.volume-frame.volume.rolling(lookback).mean())/frame.volume.rolling(lookback).std().replace(0,np.nan),
    ))
    return result.fillna(0).to_dict('records')


def classify(f, price, trend_adx=25):
    """优先处理恐慌和突破，其次趋势和波动状态。"""
    if f['atr_rank'] >= .99 and abs(f['z']) >= 3:
        return 'panic'
    if f['upper'] and (price > f['upper'] or price < f['lower']):
        return 'breakout'
    if f['adx'] >= trend_adx and abs(f['slope']) >= .5:
        return 'strong_uptrend' if f['slope'] > 0 else 'strong_downtrend'
    if f['atr_rank'] >= .95:
        return 'high_volatility'
    if f['atr_rank'] <= .1:
        return 'low_volatility'
    if f['adx'] >= trend_adx*.7 and abs(f['slope']) >= .2:
        return 'weak_uptrend' if f['slope'] > 0 else 'weak_downtrend'
    return 'range'


class AdaptiveStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbol, family='regime_adaptive', *, features=None, params=None):
        super().__init__(strategy_id, symbol)
        if family not in FAMILIES:
            raise ValueError('未知策略类型')
        self.family, self.features = family, features
        self.params = dict(params or {})
        self.fixed_center = None

    def reset_state(self):
        super().reset_state()
        self.fixed_center = None

    def compute_signals(self, bars, current_index, settings):
        if current_index < max(100, settings.lookback_period+5):
            return self._hold_signal(bars, current_index, self.family)
        # 离线预计算与逐根计算使用同一个因果函数。
        f = self.features[current_index] if self.features is not None else causal_features(bars[max(0,current_index-199):current_index+1], settings.lookback_period)[-1]
        bar = bars[current_index]
        price, atr = bar.close, f['atr']
        if atr <= 0:
            return self._hold_signal(bars, current_index, self.family)
        regime = classify(f, price, self.params.get('trend_adx', 25))
        family = self.family
        grid_on = regime in ('range', 'low_volatility') and f['adx'] < self.params.get('trend_adx',25) and abs(f['slope']) < .5 and .1 <= f['atr_rank'] < .95
        if family == 'regime_adaptive':
            family = 'adaptive_grid' if grid_on else ('breakout' if regime == 'breakout' else 'trend')
        elif family == 'trend_grid':
            family = 'adaptive_grid' if grid_on else 'trend'
        timestamp = self._get_timestamp(bars, current_index)
        confidence = min(1, max(0, .4*min(f['adx']/40,1)+.3*min(abs(f['slope'])/2,1)+.3*min(abs(f['momentum'])/8,1)))
        metadata = dict(strategy=family, regime=regime, atr=atr, grid_on=grid_on)
        if regime == 'panic' or (family != 'fixed_grid' and regime == 'high_volatility'):
            return [Signal(timestamp,self.symbol,SignalType.CLOSE_LONG,metadata=metadata),
                    Signal(timestamp,self.symbol,SignalType.CLOSE_SHORT,metadata=metadata)]
        if family in ('fixed_grid','adaptive_grid'):
            if family == 'adaptive_grid' and not grid_on:
                return [Signal(timestamp,self.symbol,SignalType.CLOSE_LONG,metadata=metadata),
                        Signal(timestamp,self.symbol,SignalType.CLOSE_SHORT,metadata=metadata)]
            if self.fixed_center is None:
                self.fixed_center = (f['center'], atr)
            center, scale = self.fixed_center if family == 'fixed_grid' else (f['center'], atr)
            levels = self.params.get('levels', 3)
            if family == 'adaptive_grid' and f['atr_rank'] > .75:
                levels = max(1, levels//2)
            spacing = scale*self.params.get('spacing',1.0)
            signals = []
            for level in range(1, levels+1):
                for direction in (1,-1):
                    limit = center-direction*spacing*level
                    if limit <= 0 or (direction == 1 and limit >= price) or (direction == -1 and limit <= price):
                        continue
                    signals.append(Signal(timestamp,self.symbol,SignalType.BUY if direction==1 else SignalType.SELL,
                        price=limit, stop_loss=limit-direction*atr*settings.stop_loss_atr,
                        take_profit=limit+direction*spacing, confidence=max(.3, 1-abs(f['slope'])),
                        metadata={**metadata,'order_type':'limit','grid_key':f'{direction}:{level}',
                                  'grid_levels':levels*2,'grid_center':center,'grid_spacing':spacing}))
            return signals
        direction = 0
        if family == 'trend':
            if f['adx'] >= self.params.get('trend_adx',25)*.7 and abs(f['momentum']) >= settings.entry_threshold:
                direction = 1 if f['slope']>0 and f['momentum']>0 else (-1 if f['slope']<0 and f['momentum']<0 else 0)
        elif family == 'breakout':
            direction = 1 if price > f['upper'] else (-1 if price < f['lower'] else 0)
        elif family == 'mean_reversion':
            # 均值回归组保持纯粹，不偷偷切换为趋势策略。
            direction = -1 if settings.entry_threshold <= f['z'] < 3 else (1 if -3 < f['z'] <= -settings.entry_threshold else 0)
            confidence = min(1, abs(f['z'])/3)
        if direction and self.state.position*direction <= 0:
            return [Signal(timestamp,self.symbol,SignalType.BUY if direction==1 else SignalType.SELL,
                price=price, stop_loss=price-direction*atr*settings.stop_loss_atr,
                take_profit=f['center'] if family=='mean_reversion' else price+direction*atr*settings.take_profit_atr,
                confidence=confidence, metadata=metadata)]
        if self.state.position and ((family=='mean_reversion' and self.state.position*(price-f['center'])>=0) or
                                    (family!='mean_reversion' and self.state.position*f['slope']<0)):
            return [Signal(timestamp,self.symbol,SignalType.CLOSE_LONG if self.state.position>0 else SignalType.CLOSE_SHORT,metadata=metadata)]
        return self._hold_signal(bars,current_index,self.family)
