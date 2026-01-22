"""Strategy factory helpers."""

from __future__ import annotations

import inspect
from typing import Any, Dict, Set

from core.strategy_lib import GridStrategy

from .config import normalize_config

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
