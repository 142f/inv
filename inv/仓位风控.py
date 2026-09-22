"""三种运行模式共用的仓位预算和权益风控，不依赖券商。"""
from dataclasses import dataclass
from math import isfinite
from inv.domain.models import Signal, SymbolSpec


@dataclass(frozen=True)
class RiskPolicy:
    max_leverage: float = 10.0
    max_margin_usage: float = .5
    max_drawdown: float = .25
    max_daily_loss: float = .05
    risk_per_trade: float = .01
    max_asset_exposure: float = 2.0
    max_portfolio_exposure: float = 3.0
    maintenance_margin: float = .5

    def __post_init__(self):
        if not 0 < self.max_leverage <= 10:
            raise ValueError("杠杆上限必须在 (0,10] 内")
        for name in ('max_margin_usage', 'max_drawdown', 'max_daily_loss', 'risk_per_trade', 'maintenance_margin'):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} 必须在 (0,1] 内")
        if not 0 < self.max_asset_exposure <= self.max_portfolio_exposure <= 10:
            raise ValueError("资产与组合敞口预算无效")


class PositionSizer:
    def __init__(self, policy: RiskPolicy):
        self.policy = policy

    def size(self, signal: Signal, price: float, equity: float, spec: SymbolSpec,
             gross_notional: float = 0, peak: float | None = None,
             portfolio_notional: float | None = None, risk_fraction: float | None = None) -> float:
        stop = signal.stop_loss
        if risk_fraction is not None and (not isfinite(risk_fraction) or risk_fraction <= 0):
            return 0.0
        if not isfinite(signal.volume) or signal.volume < 0:
            return 0.0
        if not all(isfinite(x) for x in (price, equity, gross_notional, signal.confidence)) or price <= 0 or equity <= 0:
            return 0.0
        if stop is None or not isfinite(stop) or abs(price-stop) < spec.price_tick:
            return 0.0
        confidence = min(1.0, max(0.0, signal.confidence))
        drawdown = max(0.0, 1-equity/max(peak or equity, equity))
        reduction = max(0.0, 1-drawdown/self.policy.max_drawdown)
        budget = equity * min(self.policy.risk_per_trade, risk_fraction or self.policy.risk_per_trade)
        # 网格总风险由所有层共享，禁止每层重复使用完整预算。
        budget *= confidence * reduction / max(1, int(signal.metadata.get('grid_levels', 1)))
        multiplier = spec.tick_value/spec.tick_size if spec.tick_value > 0 else spec.contract_size
        volume = budget / (abs(price-stop)*multiplier)
        requested_leverage = min(self.policy.max_leverage, .5 + 3.5*confidence*reduction)
        asset_room = equity*min(requested_leverage, self.policy.max_asset_exposure)-gross_notional
        portfolio_room = equity*min(self.policy.max_portfolio_exposure,
                                    self.policy.max_leverage*self.policy.max_margin_usage)
        portfolio_room -= portfolio_notional if portfolio_notional is not None else gross_notional
        volume = min(volume, max(0, min(asset_room, portfolio_room))/(price*spec.contract_size))
        if signal.volume > 0:
            volume = min(volume, signal.volume)
        return spec.normalize_volume(volume)


@dataclass
class RiskState:
    peak: float
    day_equity: float
    day: int | None = None
    halted: bool = False
    reason: str = ''
    previous_equity: float | None = None
    daily_halted: bool = False

    def assess(self, equity: float, timestamp: float, policy: RiskPolicy) -> bool:
        if not isfinite(equity) or equity <= 0:
            self.halted, self.reason = True, '权益无效'
        day = int(timestamp//86400)
        if self.day != day:
            self.day = day
            self.day_equity = self.previous_equity if self.previous_equity is not None else self.day_equity
            self.daily_halted = False
            if not self.halted: self.reason = ''
        self.previous_equity = equity
        self.peak = max(self.peak, equity)
        if equity <= self.peak*(1-policy.max_drawdown):
            self.halted, self.reason = True, '最大回撤触发'
        if equity <= self.day_equity*(1-policy.max_daily_loss):
            self.reason = '当日亏损触发'
            self.daily_halted = True
        return not (self.halted or self.daily_halted)
