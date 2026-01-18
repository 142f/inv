"""Strategy configuration loader and validator."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import yaml

from core.logger import Logger


class ConfigValidationError(Exception):
    pass


class ConfigLoader:
    REQUIRED_FIELDS = ["symbol", "step", "tp_dist", "lot", "magic"]

    def __init__(self, config_path: Path | None = None):
        project_root = Path(__file__).resolve().parents[2]
        default_path = project_root / "config" / "strategies.yaml"
        self.config_path = Path(config_path) if config_path else default_path
        self.last_mtime = 0.0

    def load_if_changed(self) -> Tuple[bool, List[dict]]:
        """Return (changed, configs). If unchanged, configs is empty."""
        try:
            current_mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            Logger.log("SYSTEM", "WARN", f"Config file not found: {self.config_path}")
            return False, []

        if current_mtime <= self.last_mtime:
            return False, []

        configs = self._load_configs()
        self.last_mtime = current_mtime
        return True, configs

    def force_load(self) -> List[dict]:
        configs = self._load_configs()
        try:
            self.last_mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            pass
        return configs

    def _load_configs(self) -> List[dict]:
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or []
        except Exception as exc:
            Logger.log("SYSTEM", "ERROR", f"Failed to read config: {exc}")
            return []

        if not isinstance(data, list):
            Logger.log("SYSTEM", "ERROR", "Config file root must be a list.")
            return []

        return self._validate_all(data)

    def _validate_all(self, items: List[dict]) -> List[dict]:
        validated: List[dict] = []
        seen_magic: set[int] = set()

        for idx, cfg in enumerate(items):
            try:
                self._validate_single(cfg, seen_magic)
                validated.append(cfg)
            except ConfigValidationError as exc:
                Logger.log("SYSTEM", "CONFIG_ERROR", f"Config #{idx + 1} invalid: {exc}")
                continue

        return validated

    def _validate_single(self, cfg: dict, seen_magic: set[int]):
        if not isinstance(cfg, dict):
            raise ConfigValidationError("Config entry must be a mapping.")

        missing = [f for f in self.REQUIRED_FIELDS if f not in cfg]
        if missing:
            raise ConfigValidationError(f"Missing required fields: {', '.join(missing)}")

        magic = cfg.get("magic")
        if magic in seen_magic:
            raise ConfigValidationError(f"Duplicate magic: {magic}")
        seen_magic.add(magic)

        self._ensure_positive(cfg, "step")
        self._ensure_positive(cfg, "tp_dist")
        self._ensure_positive(cfg, "lot")
        self._ensure_positive(cfg, "window", allow_zero=False)

        min_p = float(cfg.get("min_p", 0))
        max_p = float(cfg.get("max_p", 0))
        if min_p >= max_p:
            raise ConfigValidationError(f"min_p({min_p}) must be less than max_p({max_p})")

    def _ensure_positive(self, cfg: dict, key: str, allow_zero: bool = False):
        if key not in cfg:
            raise ConfigValidationError(f"Missing field {key}")
        try:
            value = float(cfg[key])
        except Exception:
            raise ConfigValidationError(f"Field {key} is not numeric")
        if value < 0 or (not allow_zero and value <= 0):
            raise ConfigValidationError(f"Field {key} must be greater than 0")
