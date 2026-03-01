"""Manage strategy lifecycle and config synchronization."""

from __future__ import annotations

import inspect
from typing import Any, Dict, Set

from core.logger import Logger
from core.runtime.datafeed import DataFeed
from core.strategy_lib import GridStrategy

from .config import normalize_config
from .updater import StrategyUpdater

_ALLOWED_KEYS: Set[str] | None = None

def _get_allowed_keys() -> Set[str]:
    global _ALLOWED_KEYS
    if _ALLOWED_KEYS is None:
        params = inspect.signature(GridStrategy.__init__).parameters
        _ALLOWED_KEYS = {name for name in params if name != "self"}
    return _ALLOWED_KEYS

def build_strategy(cfg: Dict[str, Any], *, lock: Any = None, datafeed: Any = None) -> GridStrategy:
    normalized = normalize_config(cfg)
    allowed = _get_allowed_keys()
    kwargs = {key: normalized[key] for key in allowed if key in normalized}
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
        self._updater = StrategyUpdater(broker)
        self.datafeed = datafeed or DataFeed(broker)

    def sync(self):
        configs = self._load_changed_configs()
        if configs is None:
            return

        new_magics = self._sync_configs(configs)
        self._remove_inactive_strategies(new_magics)

    def _load_changed_configs(self) -> list[dict] | None:
        changed, configs = self.config_loader.load_if_changed()
        if not changed:
            return None

        if configs is None:
            Logger.log("SYSTEM", "WARN", f"Config load returned None: {self.config_loader.config_path}")
            return None

        if not configs:
            Logger.log(
                "SYSTEM",
                "WARN",
                f"Config file is empty or has no valid strategies: {self.config_loader.config_path}",
            )
            return None

        return configs

    @staticmethod
    def _extract_magic(cfg: dict) -> int | None:
        magic = cfg.get("magic") if isinstance(cfg, dict) else None
        if magic is None:
            Logger.log("SYSTEM", "CONFIG_ERROR", "Config missing magic; skipping entry.")
            return None
        return magic

    def _sync_configs(self, configs: list[dict]) -> set[int]:
        new_magics: set[int] = set()
        for cfg in configs:
            magic = self._extract_magic(cfg)
            if magic is None:
                continue
            new_magics.add(magic)
            self._upsert_strategy(magic, cfg)
        return new_magics

    def _upsert_strategy(self, magic: int, cfg: dict):
        strategy = self.active.get(magic)
        if strategy is None:
            self._add_strategy(cfg)
            return
        self._updater.apply(strategy, cfg)

    def _remove_inactive_strategies(self, new_magics: set[int]):
        for magic in tuple(self.active):
            if magic not in new_magics:
                self._remove_strategy(magic)

    def _add_strategy(self, cfg: dict):
        Logger.log(
            "SYSTEM",
            "ADD",
            f"Add strategy {cfg.get('symbol')} (Magic: {cfg.get('magic')}, Enabled: {cfg.get('enabled', True)})",
        )
        symbol = cfg.get("symbol")
        if not self.broker.ensure_symbol(symbol):
            Logger.log("SYSTEM", "ERROR", f"Symbol unavailable: {symbol}")
            return
        strategy = build_strategy(cfg, lock=self.broker.lock, datafeed=self.datafeed)
        self.active[cfg["magic"]] = strategy
        strategy.clear_old_orders()

    def _remove_strategy(self, magic: int):
        strategy = self.active.pop(magic, None)
        if not strategy:
            return
        Logger.log("SYSTEM", "REMOVE", f"Remove strategy {strategy.symbol} (Magic: {magic})")
        strategy.clear_old_orders()
