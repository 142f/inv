from dataclasses import dataclass, field
from typing import Optional, Literal

@dataclass
class GridParams:
    symbol: str
    step: float
    tp_dist: float
    sl_dist: float
    lot: float
    magic: int
    window: int
    min_price: float
    max_price: float
    mode: str = "neutral"
    buy_window: Optional[int] = None
    sell_window: Optional[int] = None
    out_of_range_action: str = "freeze"

@dataclass
class AtrParams:
    use_atr: bool = False
    atr_period: int = 14
    atr_factor: float = 1.0
    atr_mode: str = "wilder"
    atr_timeframe: str = "M15"
    atr_update_seconds: int = 5
    atr_smooth: float = 0.1
    atr_change_threshold: float = 0.01
    min_step_mult: float = 0.5
    max_step_mult: float = 3.0

@dataclass
class AdaptiveParams:
    enabled: bool = False
    timeframe: str = "M15"
    lookback: int = 200
    quantile_low: float = 0.30
    quantile_high: float = 0.70
    step_mult_low: float = 0.90
    step_mult_high: float = 1.10
    lot_min_mult: float = 0.50
    lot_max_mult: float = 1.50
    range_buffer_atr: float = 1.0

@dataclass
class CapsParams:
    max_long_pos: Optional[int] = None
    max_short_pos: Optional[int] = None
    max_long_vol: Optional[float] = None
    max_short_vol: Optional[float] = None
    max_net_vol: Optional[float] = None
    max_gross_vol: Optional[float] = None

@dataclass
class HedgeParams:
    enabled: bool = False
    fraction: float = 0.3333
    tranches: int = 3
    entry_steps: int = 1
    exit_steps: int = 1
    cooldown: float = 20.0
    
    # Gates
    vol_lookback: int = 300
    vol_window: int = 20
    vol_quantile: float = 0.90
    vol_base: int = 200
    vol_mult: float = 3.0

    # Break-even
    be_trigger_steps: int = 1
    be_buffer_points: int = 20

@dataclass
class AnchorParams:
    anchor: Optional[float] = None
    recenter_steps: int = 3
    recenter_cooldown: float = 30.0

@dataclass
class ExtremeParams:
    max_spread_points: Optional[float] = None
    extreme_mode: str = "freeze"
    extreme_cooldown: float = 30.0

@dataclass
class ThrottleParams:
    max_new_orders_per_update: int = 10
    auto_trim: bool = False
