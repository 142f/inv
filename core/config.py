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
    ALLOWED_MODES = {"neutral", "long", "short"}
    ALLOWED_OUT_OF_RANGE_ACTIONS = {"freeze", "stop"}
    ALLOWED_ATR_MODES = {"wilder", "ema", "sma"}
    ALLOWED_TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
    BOOL_FIELDS = ("enabled", "use_atr", "adaptive_enabled", "hedge_enabled", "auto_trim")

    def __init__(self, config_path: Path | None = None):
        project_root = Path(__file__).resolve().parents[1]
        default_path = project_root / "config" / "strategies.yaml"
        self.config_path = Path(config_path) if config_path else default_path
        self.last_mtime = 0.0
        self.last_load_failed = False

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
        if configs is None:
            # Keep last_mtime unchanged so next sync will retry loading the same file.
            self.last_load_failed = True
            return False, []

        self.last_load_failed = False
        self.last_mtime = current_mtime
        return True, configs

    def force_load(self) -> List[dict]:
        configs = self._load_configs()
        if configs is None:
            self.last_load_failed = True
            return []
        self.last_load_failed = False
        try:
            self.last_mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            pass
        return configs

    def _load_configs(self) -> List[dict] | None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or []
        except Exception as exc:
            Logger.log("SYSTEM", "ERROR", f"Failed to read config {self.config_path}: {exc}")
            return None

        if not isinstance(data, list):
            Logger.log("SYSTEM", "ERROR", f"Config root must be a list: {self.config_path}")
            return None

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

        magic = self._parse_magic(cfg.get("magic"))
        if magic in seen_magic:
            raise ConfigValidationError(f"Duplicate magic: {magic}")
        seen_magic.add(magic)
        cfg["magic"] = magic
        self._normalize_bool_fields(cfg)

        mode = cfg.get("mode")
        if mode is not None:
            normalized_mode = str(mode).strip().lower()
            if normalized_mode not in self.ALLOWED_MODES:
                raise ConfigValidationError(f"Invalid mode: {mode}")
            cfg["mode"] = normalized_mode

        out_of_range_action = cfg.get("out_of_range_action")
        if out_of_range_action is not None:
            normalized_action = str(out_of_range_action).strip().lower()
            if normalized_action not in self.ALLOWED_OUT_OF_RANGE_ACTIONS:
                raise ConfigValidationError(f"Invalid out_of_range_action: {out_of_range_action}")
            cfg["out_of_range_action"] = normalized_action

        atr_mode = cfg.get("atr_mode")
        if atr_mode is not None:
            normalized_atr_mode = str(atr_mode).strip().lower()
            if normalized_atr_mode not in self.ALLOWED_ATR_MODES:
                raise ConfigValidationError(f"Invalid atr_mode: {atr_mode}")
            cfg["atr_mode"] = normalized_atr_mode

        for tf_key in ("atr_timeframe", "adaptive_timeframe"):
            if tf_key in cfg and cfg[tf_key] is not None:
                tf = str(cfg[tf_key]).strip().upper()
                if tf not in self.ALLOWED_TIMEFRAMES:
                    raise ConfigValidationError(f"Invalid {tf_key}: {cfg[tf_key]}")
                cfg[tf_key] = tf

        self._ensure_positive(cfg, "step")
        self._ensure_positive(cfg, "tp_dist")
        self._ensure_positive(cfg, "lot")
        # window 是可选字段，有默认值，只在配置了的情况下验证
        if "window" in cfg:
            self._ensure_positive(cfg, "window", allow_zero=False)
        if "buy_window" in cfg:
            self._ensure_positive(cfg, "buy_window", allow_zero=False)
        if "sell_window" in cfg:
            self._ensure_positive(cfg, "sell_window", allow_zero=False)
        if "max_new_orders_per_update" in cfg:
            self._ensure_positive(cfg, "max_new_orders_per_update", allow_zero=False)

        self._ensure_positive(cfg, "atr_period")
        self._ensure_positive(cfg, "atr_factor")
        self._ensure_positive(cfg, "max_net_vol")
        self._ensure_positive(cfg, "max_long_vol")
        self._ensure_positive(cfg, "max_short_vol")
        self._ensure_positive(cfg, "max_gross_vol")
        self._ensure_positive(cfg, "hedge_tranches")
        self._ensure_positive(cfg, "hedge_entry_steps")
        self._ensure_positive(cfg, "hedge_exit_steps")
        self._ensure_positive(cfg, "hedge_cooldown")
        self._ensure_positive(cfg, "hedge_vol_lookback")
        self._ensure_positive(cfg, "hedge_vol_window")
        self._ensure_positive(cfg, "hedge_vol_base")
        self._ensure_positive(cfg, "be_trigger_steps")
        self._ensure_positive(cfg, "be_buffer_points", allow_zero=True)

        self._ensure_non_negative(cfg, "atr_update_seconds")
        self._ensure_non_negative(cfg, "atr_change_threshold")
        self._ensure_non_negative(cfg, "recenter_steps")
        self._ensure_non_negative(cfg, "recenter_cooldown")
        self._ensure_non_negative(cfg, "extreme_cooldown")
        self._ensure_non_negative(cfg, "max_spread_points")
        self._ensure_non_negative(cfg, "utility_cost_weight")
        self._ensure_non_negative(cfg, "utility_distance_weight")
        self._ensure_non_negative(cfg, "utility_risk_weight")

        self._ensure_between(cfg, "hedge_fraction", low=0.0, high=1.0, inclusive=True)
        self._ensure_between(cfg, "hedge_vol_quantile", low=0.0, high=1.0, inclusive=True)
        self._ensure_between(cfg, "adaptive_quantile_low", low=0.0, high=1.0, inclusive=True)
        self._ensure_between(cfg, "adaptive_quantile_high", low=0.0, high=1.0, inclusive=True)

        if "adaptive_quantile_low" in cfg and "adaptive_quantile_high" in cfg:
            q_low = float(cfg["adaptive_quantile_low"])
            q_high = float(cfg["adaptive_quantile_high"])
            if q_low >= q_high:
                raise ConfigValidationError(
                    f"adaptive_quantile_low({q_low}) must be less than adaptive_quantile_high({q_high})"
                )

        min_p = float(cfg.get("min_p", 0))
        max_p = float(cfg.get("max_p", 0))
        if min_p >= max_p:
            raise ConfigValidationError(f"min_p({min_p}) must be less than max_p({max_p})")

    def _ensure_positive(self, cfg: dict, key: str, allow_zero: bool = False):
        if key not in cfg:
            # 可选字段不存在时跳过验证
            return
        if self._is_empty_optional_value(cfg, key):
            return
        try:
            value = float(cfg[key])
        except Exception:
            raise ConfigValidationError(f"Field {key} is not numeric")
        if value < 0 or (not allow_zero and value <= 0):
            raise ConfigValidationError(f"Field {key} must be greater than 0")

    @staticmethod
    def _parse_magic(value) -> int:
        try:
            magic = int(float(value))
        except Exception:
            raise ConfigValidationError(f"Field magic is not numeric: {value}")
        if magic <= 0:
            raise ConfigValidationError(f"Field magic must be greater than 0: {value}")
        return magic

    @classmethod
    def _normalize_bool_fields(cls, cfg: dict) -> None:
        for key in cls.BOOL_FIELDS:
            if key not in cfg:
                continue
            if cfg[key] is None or (isinstance(cfg[key], str) and not cfg[key].strip()):
                continue
            cfg[key] = cls._parse_bool(cfg[key], key)

    @staticmethod
    def _parse_bool(value, key: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if float(value) in (0.0, 1.0):
                return bool(int(value))
            raise ConfigValidationError(f"Field {key} must be boolean-like (0/1/true/false), got: {value}")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off", ""}:
                return False
            raise ConfigValidationError(f"Field {key} has invalid boolean value: {value!r}")
        raise ConfigValidationError(f"Field {key} has unsupported boolean type: {type(value).__name__}")

    def _ensure_non_negative(self, cfg: dict, key: str):
        if key not in cfg:
            return
        if self._is_empty_optional_value(cfg, key):
            return
        try:
            value = float(cfg[key])
        except Exception:
            raise ConfigValidationError(f"Field {key} is not numeric")
        if value < 0:
            raise ConfigValidationError(f"Field {key} must be >= 0")

    def _ensure_between(self, cfg: dict, key: str, *, low: float, high: float, inclusive: bool):
        if key not in cfg:
            return
        if self._is_empty_optional_value(cfg, key):
            return
        try:
            value = float(cfg[key])
        except Exception:
            raise ConfigValidationError(f"Field {key} is not numeric")
        if inclusive:
            if value < low or value > high:
                raise ConfigValidationError(f"Field {key} must be in [{low}, {high}]")
        else:
            if value <= low or value >= high:
                raise ConfigValidationError(f"Field {key} must be in ({low}, {high})")

    def _is_empty_optional_value(self, cfg: dict, key: str) -> bool:
        if key in self.REQUIRED_FIELDS:
            return False
        value = cfg.get(key)
        return value is None or (isinstance(value, str) and not value.strip())
