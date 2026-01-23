"""Manage strategy lifecycle and config synchronization."""

from __future__ import annotations

from typing import Dict

from core.logger import Logger
from core.runtime.datafeed import DataFeed
from core.strategy_lib import GridStrategy

from .factory import build_strategy
from .updater import StrategyUpdater


class StrategyManager:
    def __init__(self, broker, config_loader, datafeed: DataFeed | None = None):
        self.broker = broker
        self.config_loader = config_loader
        self.active: Dict[int, GridStrategy] = {}
        self._updater = StrategyUpdater(broker)
        self.datafeed = datafeed or DataFeed(broker)

    def sync(self):
        changed, configs = self.config_loader.load_if_changed()
        if not changed:
            return

        if configs is None:
            Logger.log("SYSTEM", "WARN", f"Config load returned None: {self.config_loader.config_path}")
            return

        if not configs:
            Logger.log(
                "SYSTEM",
                "WARN",
                f"Config file is empty or has no valid strategies: {self.config_loader.config_path}",
            )
            return

        new_magics = [cfg.get("magic") for cfg in configs if isinstance(cfg, dict)]

        for cfg in configs:
            magic = cfg.get("magic") if isinstance(cfg, dict) else None
            if magic is None:
                Logger.log("SYSTEM", "CONFIG_ERROR", "Config missing magic; skipping entry.")
                continue

            if magic not in self.active:
                self._add_strategy(cfg)
            else:
                self._updater.apply(self.active[magic], cfg)

        for magic in list(self.active.keys()):
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
