"""Initialization helpers for GridStrategy."""

from __future__ import annotations

import time


def as_optional(value, cast):
    return cast(value) if value is not None else None


def build_stats_state(magic: int) -> dict:
    return {
        "magic": magic,
        "start_time": time.time(),
        "last_reset_time": time.time(),
        "long_profitable_count": 0,
        "long_profitable_amount": 0.0,
        "short_profitable_count": 0,
        "short_profitable_amount": 0.0,
        "last_stats_update_time": 0,
    }

