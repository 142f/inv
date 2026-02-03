from dataclasses import dataclass, field
from typing import Dict, Optional, Any
import time

@dataclass
class SymbolCache:
    digits: int = 0
    point: float = 0.0
    stop_level: float = 0.0
    vol_min: float = 0.0
    vol_max: float = 0.0
    vol_step: float = 0.0
    initialized: bool = False

@dataclass
class RuntimeState:
    last_tick_time: float = 0.0
    last_atr_value: Optional[float] = None
    last_atr_time: float = 0.0
    last_status_log_time: float = 0.0
    pause_until: float = 0.0
    
    # Anchor state
    anchor: Optional[float] = None
    last_recenter_time: float = 0.0
    
    # Hedge state
    last_hedge_time: float = 0.0
    last_hedge_entry_price: Optional[float] = None

@dataclass
class StatsState:
    magic: int
    start_time: float = field(default_factory=time.time)
    last_reset_time: float = field(default_factory=time.time)
    
    long_profitable_count: int = 0
    long_profitable_amount: float = 0.0
    short_profitable_count: int = 0
    short_profitable_amount: float = 0.0
    
    last_stats_update_time: float = 0.0
    
    # Internal tracking
    order_profit: Dict[int, float] = field(default_factory=dict)
    order_type: Dict[int, str] = field(default_factory=dict)
    last_deal_time: float = 0.0
    last_deal_ticket: int = 0
