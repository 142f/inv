"""Simple in-memory runtime state repository."""

from __future__ import annotations

from typing import Any, Dict


class InMemoryStateRepository:
    def __init__(self):
        self._states: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Dict[str, Any] | None:
        return self._states.get(str(key))

    def set(self, key: str, state: Dict[str, Any]) -> None:
        self._states[str(key)] = dict(state)

    def delete(self, key: str) -> None:
        self._states.pop(str(key), None)

