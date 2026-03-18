"""策略生命周期管理：配置归一化、热更新与策略管理器。"""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable, Dict, FrozenSet

from core.logger import Logger
from core.runtime import DataFeed
from core.strategy.grid_strategy.strategy import GridStrategy

# ── 配置归一化 (Config normalization) ──────────────────────────────────────────

_BOOL_KEYS = frozenset({
    "enabled", "use_atr", "adaptive_enabled", "hedge_enabled", "auto_trim",
})

_INT_KEYS = frozenset({
    "magic", "window", "atr_period", "buy_window", "sell_window",
    "recenter_steps", "max_long_pos", "max_short_pos", 
    "max_new_orders_per_update", "hedge_tranches", "hedge_entry_steps",
    "hedge_exit_steps", "hedge_vol_lookback", "hedge_vol_window",
    "hedge_vol_base", "be_trigger_steps", "be_buffer_points", "adaptive_lookback",
})

_FLOAT_KEYS = frozenset({
    "step", "tp_dist", "sl_dist", "lot", "min_p", "max_p", "atr_factor",
    "atr_update_seconds", "atr_smooth", "atr_change_threshold", "min_step_mult",
    "max_step_mult", "adaptive_quantile_low", "adaptive_quantile_high",
    "adaptive_step_mult_low", "adaptive_step_mult_high", "adaptive_lot_min_mult",
    "adaptive_lot_max_mult", "adaptive_range_buffer_atr", "recenter_cooldown",
    "max_long_vol", "max_short_vol", "max_net_vol", "max_spread_points",
    "extreme_cooldown", "hedge_fraction", "hedge_cooldown", "max_gross_vol",
    "hedge_vol_quantile", "hedge_vol_mult",
})


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
        # Fail-safe: unknown non-empty strings should not silently enable features.
        return False
    return bool(value)


def _coerce_int(value: Any) -> Any:
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return value


def _coerce_float(value: Any) -> Any:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return value


def _build_coercers_by_key() -> Dict[str, Callable[[Any], Any]]:
    coercers: Dict[str, Callable[[Any], Any]] = {}
    coercers.update({key: _coerce_bool for key in _BOOL_KEYS})
    coercers.update({key: _coerce_int for key in _INT_KEYS})
    coercers.update({key: _coerce_float for key in _FLOAT_KEYS})
    return coercers

_COERCERS_BY_KEY = _build_coercers_by_key()


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        return {}

    normalized: Dict[str, Any] = {}
    for key, value in cfg.items():
        coercer = _COERCERS_BY_KEY.get(key)
        normalized[key] = coercer(value) if coercer is not None else value
    return normalized


# ── 策略热更新 (Strategy Updater) ──────────────────────────────────────────────

class StrategyUpdater:
    _ATR_KEYS = (
        "use_atr", "atr_period", "atr_factor", "atr_mode", "atr_timeframe",
        "atr_update_seconds", "atr_smooth", "atr_change_threshold",
        "min_step_mult", "max_step_mult",
    )

    _ADAPTIVE_KEYS = (
        "adaptive_enabled", "adaptive_timeframe", "adaptive_lookback",
        "adaptive_quantile_low", "adaptive_quantile_high", "adaptive_step_mult_low",
        "adaptive_step_mult_high", "adaptive_lot_min_mult", "adaptive_lot_max_mult",
        "adaptive_range_buffer_atr",
    )

    _GENERAL_KEYS = (
        "mode", "out_of_range_action", "recenter_steps", "recenter_cooldown",
        "max_long_pos", "max_short_pos", "max_long_vol", "max_short_vol",
        "max_net_vol", "max_spread_points", "extreme_cooldown",
        "max_new_orders_per_update", "auto_trim",
    )

    _HEDGE_KEYS = (
        "hedge_enabled", "hedge_fraction", "hedge_tranches", "hedge_entry_steps",
        "hedge_exit_steps", "hedge_cooldown", "max_gross_vol", "hedge_vol_lookback",
        "hedge_vol_window", "hedge_vol_quantile", "hedge_vol_base", "hedge_vol_mult",
        "be_trigger_steps", "be_buffer_points",
    )

    _ORDER_RELATED_KEYS = (
        "step", "tp_dist", "sl_dist", "lot", "window", "buy_window",
        "sell_window", "min_price", "max_price", "mode", "auto_trim",
    )
    _DIRECT_KEYS = (
        "symbol", "magic", "enabled", "step", "tp_dist", "sl_dist", "lot", "window",
        "buy_window", "sell_window", "min_p", "max_p",
    )
    _KNOWN_CONFIG_KEYS = frozenset(
        _DIRECT_KEYS + _ATR_KEYS + _ADAPTIVE_KEYS + _GENERAL_KEYS + _HEDGE_KEYS
    )

    def __init__(self, broker):
        self.broker = broker

    def apply(self, strategy: GridStrategy, cfg: dict) -> bool:
        cfg = normalize_config(cfg)
        unknown_keys = [key for key in cfg.keys() if key not in self._KNOWN_CONFIG_KEYS]
        if unknown_keys:
            Logger.log(
                strategy.symbol,
                "CONFIG_ERROR",
                f"Ignored unknown config keys: {', '.join(sorted(unknown_keys))}",
            )
        current_state = strategy.get_state()
        current_state.pop("enabled", None)
        cleared_orders = False
        was_use_atr = bool(getattr(strategy, "use_atr", False))

        # 【修改点】保留订单相关字段的快照（由于逻辑分散），但移除了 ATR 与 Adaptive 的全量快照分配
        before = self._snapshot_order_related_state(strategy)

        new_symbol = cfg.get("symbol", strategy.symbol)
        if strategy.symbol != new_symbol:
            if not self.broker.ensure_symbol(new_symbol):
                Logger.log(
                    "SYSTEM",
                    "WARN",
                    f"Strategy {strategy.magic} symbol switch deferred: {strategy.symbol} -> {new_symbol} (ensure_symbol failed)",
                )
                return False
            strategy.clear_old_orders(force_all=True)
            cleared_orders = True
            strategy.set_symbol(new_symbol, reset_runtime_state=True)
            current_state = {}
            Logger.log("系统", "更新", f"策略 {strategy.magic} 交易品种变更: {before['symbol']} -> {strategy.symbol}")

        self._apply_base_updates(strategy, cfg)
        self._apply_window_and_range_updates(strategy, cfg)
        
        # 【修改点】_apply_updates 现直接返回是否发生变更，消除二次遍历
        atr_changed = self._apply_updates(strategy, cfg, self._ATR_KEYS)
        self._apply_updates(strategy, cfg, self._GENERAL_KEYS, coerce_value=self._coerce_general_value)
        self._apply_updates(strategy, cfg, self._HEDGE_KEYS)
        adaptive_changed = self._apply_updates(strategy, cfg, self._ADAPTIVE_KEYS)
        atr_disabled_now = was_use_atr and (not bool(getattr(strategy, "use_atr", False)))

        if atr_changed:
            strategy._last_atr_value = None
            strategy._last_atr_time = 0.0
        if adaptive_changed:
            strategy._last_adapt_bar_time = 0.0
        if atr_disabled_now and not cleared_orders:
            strategy.clear_old_orders(force_all=True)
            cleared_orders = True

        enabled_changed = ("enabled" in cfg) and (bool(cfg["enabled"]) != before["enabled"])
        order_related_changed = self._has_changed(strategy, before, self._ORDER_RELATED_KEYS)
        should_clear_orders = enabled_changed or order_related_changed or atr_changed or adaptive_changed

        if should_clear_orders and not cleared_orders:
            strategy.clear_old_orders()

        strategy.set_state(current_state)
        if atr_changed:
            strategy._last_atr_value = None
            strategy._last_atr_time = 0.0
        if adaptive_changed:
            strategy._last_adapt_bar_time = 0.0
        Logger.log(
            "系统", "更新",
            f"策略已更新: {strategy.symbol} (Magic: {strategy.magic}, 状态: {strategy.enabled}, "
            f"清理订单: {should_clear_orders})"
        )

        return True

    @classmethod
    def _snapshot_order_related_state(cls, strategy: GridStrategy) -> dict:
        state = {key: getattr(strategy, key, None) for key in cls._ORDER_RELATED_KEYS}
        state["symbol"] = strategy.symbol
        state["enabled"] = strategy.enabled
        return state

    @staticmethod
    def _has_changed(strategy: GridStrategy, before: dict, keys: tuple) -> bool:
        return any(getattr(strategy, key, None) != before[key] for key in keys)

    @staticmethod
    def _apply_updates(strategy: GridStrategy, cfg: dict, keys: tuple, *, coerce_value=None) -> bool:
        """【修改点】合并修改与对比逻辑，返回布尔值表示是否有字段变更。降低空间复杂度。"""
        changed = False
        for key in keys:
            if key not in cfg:
                continue
            if not hasattr(strategy, key):
                Logger.log(strategy.symbol, "CONFIG_ERROR", f"Ignored unsupported key in hot update: {key}")
                continue
            value = cfg[key]
            if coerce_value is not None:
                value = coerce_value(strategy, key, value)
            
            if getattr(strategy, key, None) != value:
                setattr(strategy, key, value)
                changed = True
        return changed

    @staticmethod
    def _apply_base_updates(strategy: GridStrategy, cfg: dict):
        """【修改点】移除冗余的 'in cfg' 和 '.get() is not None' 双重检查。"""
        if "enabled" in cfg:
            strategy.enabled = bool(cfg["enabled"])

        step = cfg.get("step")
        if step is not None:
            new_step = float(step)
            # [修复 L-09] 拒绝 step <= 0，防止下游出现除零错误或负数网格
            if new_step <= 0:
                Logger.log(strategy.symbol, "CONFIG_ERROR",
                    f"step={new_step} 必须为正数，已忽略该配置项")
            elif new_step != strategy.step:
                strategy.step = strategy.base_step = new_step

        tp_dist = cfg.get("tp_dist")
        if tp_dist is not None:
            strategy.tp_dist = strategy.base_tp_dist = tp_dist

        sl_dist = cfg.get("sl_dist")
        if sl_dist is not None:
            strategy.sl_dist = sl_dist

        lot = cfg.get("lot")
        if lot is not None:
            strategy.lot = strategy.base_lot = lot

    @staticmethod
    def _apply_window_and_range_updates(strategy: GridStrategy, cfg: dict):
        """【修改点】精简变量访问逻辑，优化分支效率。"""
        window_changed = False
        window = cfg.get("window")
        if window is not None:
            new_window = int(window)
            if new_window != strategy.window:
                strategy.window = new_window
                window_changed = True

        min_p = cfg.get("min_p")
        if min_p is not None:
            strategy.min_price = min_p
            # [修复 L-06] 热更新时同步刷新硬边界，adaptive 的 clamp 基准随之更新。
            strategy._user_min_price = float(min_p)

        max_p = cfg.get("max_p")
        if max_p is not None:
            strategy.max_price = max_p
            strategy._user_max_price = float(max_p)

        buy_window = cfg.get("buy_window")
        if buy_window is not None:
            strategy.buy_window = int(buy_window)
        elif window_changed:
            strategy.buy_window = strategy.window

        sell_window = cfg.get("sell_window")
        if sell_window is not None:
            strategy.sell_window = int(sell_window)
        elif window_changed:
            strategy.sell_window = strategy.window

    @classmethod
    def _coerce_general_value(cls, strategy: GridStrategy, key: str, value):
        if key == "mode":
            return strategy._normalize_mode(value)
        return value


# ── 策略管理器 (Strategy Manager) ──────────────────────────────────────────────

# 【修改点】在模块加载时静态固化 ALLOWED_KEYS，消除运行时的动态属性分支预测开销
_ALLOWED_STRATEGY_KWARGS: FrozenSet[str] = frozenset(
    name for name in inspect.signature(GridStrategy.__init__).parameters if name != "self"
)
_PENDING_ADD_RETRY_SECONDS = 10.0
_PENDING_UPDATE_RETRY_SECONDS = 10.0

def build_strategy(cfg: Dict[str, Any], *, lock: Any = None, datafeed: Any = None) -> GridStrategy:
    normalized = normalize_config(cfg)
    kwargs = {key: normalized[key] for key in _ALLOWED_STRATEGY_KWARGS if key in normalized}
    if lock is not None:
        kwargs["lock"] = lock
    if datafeed is not None:
        kwargs["datafeed"] = datafeed
    return GridStrategy(**kwargs)


class StrategyManager:
    def __init__(self, broker, config_loader, datafeed: DataFeed | None = None):
        self.broker = broker
        self.config_loader = config_loader
        self.active: Dict[int, GridStrategy] = {}
        self._pending_additions: Dict[int, dict] = {}
        self._pending_retry_at: Dict[int, float] = {}
        self._pending_updates: Dict[int, dict] = {}
        self._pending_update_retry_at: Dict[int, float] = {}
        self._updater = StrategyUpdater(broker)
        self.datafeed = datafeed or DataFeed(broker)

    def sync(self):
        configs = self._load_changed_configs()
        if configs is not None:
            new_magics = self._sync_configs(configs)
            self._remove_inactive_strategies(new_magics)

        self._retry_pending_additions()
        self._retry_pending_updates()

    def _load_changed_configs(self) -> list[dict] | None:
        """【修改点】重构配置判空逻辑，合并冗余的 None 判断结构。"""
        changed, configs = self.config_loader.load_if_changed()
        if not changed:
            return None

        if not configs:  # 自动涵盖了 configs is None 和 len(configs) == 0
            Logger.log("系统", "警告", f"配置文件为空或未加载到有效策略: {self.config_loader.config_path}")
            # 配置已变更但无有效策略：视为“下线全部策略”
            return []

        return configs

    @staticmethod
    def _extract_magic(cfg: dict) -> int | None:
        magic = cfg.get("magic") if isinstance(cfg, dict) else None
        if magic is None:
            Logger.log("系统", "配置错误", "策略配置缺失 magic 字段，跳过加载。")
            return None
        try:
            parsed = int(float(magic))
        except (TypeError, ValueError):
            Logger.log("SYSTEM", "CONFIG_ERROR", f"Invalid magic value: {magic}")
            return None
        if parsed <= 0:
            Logger.log("SYSTEM", "CONFIG_ERROR", f"magic must be > 0: {magic}")
            return None
        return parsed

    def _sync_configs(self, configs: list[dict]) -> set[int]:
        new_magics: set[int] = set()
        for cfg in configs:
            magic = self._extract_magic(cfg)
            if magic is None:
                continue
            new_magics.add(magic)
            self._upsert_strategy(magic, cfg)
        self._prune_pending_additions(new_magics)
        self._prune_pending_updates(new_magics)
        return new_magics

    def _upsert_strategy(self, magic: int, cfg: dict):
        strategy = self.active.get(magic)
        if strategy is None:
            self._add_strategy(magic, cfg)
            return
        self._drop_pending_addition(magic)
        applied = self._updater.apply(strategy, cfg)
        if applied:
            self._drop_pending_update(magic)
        else:
            self._queue_pending_update(magic, cfg)

    def _remove_inactive_strategies(self, new_magics: set[int]):
        # Tuple 包裹是为了防止在迭代字典过程中删除键（RuntimeError）
        for magic in tuple(self.active):
            if magic not in new_magics:
                self._remove_strategy(magic)

    def _add_strategy(self, magic: int, cfg: dict, *, from_retry: bool = False) -> bool:
        if not from_retry:
            Logger.log(
                "系统", "新增",
                f"加载新策略 {cfg.get('symbol')} (Magic: {cfg.get('magic')}, 状态: {cfg.get('enabled', True)})"
            )
        symbol = cfg.get("symbol")
        if not self.broker.ensure_symbol(symbol):
            retry_msg = "将自动重试加载"
            Logger.log("系统", "WARN", f"交易品种暂不可用: {symbol} (magic={magic})，{retry_msg}")
            self._pending_additions[magic] = dict(cfg)
            self._pending_retry_at[magic] = time.monotonic() + _PENDING_ADD_RETRY_SECONDS
            return False
        normalized_cfg = dict(cfg)
        normalized_cfg["magic"] = magic
        strategy = build_strategy(normalized_cfg, lock=self.broker.lock, datafeed=self.datafeed)
        self.active[magic] = strategy
        self._drop_pending_addition(magic)
        self._drop_pending_update(magic)
        strategy.clear_old_orders(force_all=True)
        return True

    def _remove_strategy(self, magic: int):
        strategy = self.active.pop(magic, None)
        if not strategy:
            self._drop_pending_addition(magic)
            self._drop_pending_update(magic)
            return
        self._drop_pending_addition(magic)
        self._drop_pending_update(magic)
        Logger.log("系统", "移除", f"卸载并清理策略 {strategy.symbol} (Magic: {magic})")
        strategy.clear_old_orders(force_all=True)

    def _drop_pending_addition(self, magic: int) -> None:
        self._pending_additions.pop(magic, None)
        self._pending_retry_at.pop(magic, None)

    def _prune_pending_additions(self, valid_magics: set[int]) -> None:
        for magic in tuple(self._pending_additions):
            if magic not in valid_magics:
                self._drop_pending_addition(magic)

    def _retry_pending_additions(self) -> None:
        if not self._pending_additions:
            return

        now = time.monotonic()
        for magic, cfg in tuple(self._pending_additions.items()):
            if magic in self.active:
                self._drop_pending_addition(magic)
                continue

            retry_at = float(self._pending_retry_at.get(magic, 0.0) or 0.0)
            if now < retry_at:
                continue

            self._add_strategy(magic, cfg, from_retry=True)

    def _drop_pending_update(self, magic: int) -> None:
        self._pending_updates.pop(magic, None)
        self._pending_update_retry_at.pop(magic, None)

    def _queue_pending_update(self, magic: int, cfg: dict) -> None:
        self._pending_updates[magic] = dict(cfg)
        self._pending_update_retry_at[magic] = time.monotonic() + _PENDING_UPDATE_RETRY_SECONDS

    def _prune_pending_updates(self, valid_magics: set[int]) -> None:
        for magic in tuple(self._pending_updates):
            if magic not in valid_magics:
                self._drop_pending_update(magic)

    def _retry_pending_updates(self) -> None:
        if not self._pending_updates:
            return

        now = time.monotonic()
        for magic, cfg in tuple(self._pending_updates.items()):
            strategy = self.active.get(magic)
            if strategy is None:
                self._drop_pending_update(magic)
                continue

            retry_at = float(self._pending_update_retry_at.get(magic, 0.0) or 0.0)
            if now < retry_at:
                continue

            applied = self._updater.apply(strategy, cfg)
            if applied:
                self._drop_pending_update(magic)
            else:
                self._pending_update_retry_at[magic] = now + _PENDING_UPDATE_RETRY_SECONDS
