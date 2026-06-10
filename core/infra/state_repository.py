"""策略运行状态仓库。"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict
from pathlib import Path


class InMemoryStateRepository:
    def __init__(self):
        self._states: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Dict[str, Any] | None:
        return self._states.get(str(key))

    def set(self, key: str, state: Dict[str, Any]) -> None:
        self._states[str(key)] = dict(state)

    def delete(self, key: str) -> None:
        self._states.pop(str(key), None)


class FileStateRepository:
    """基于 JSON 文件的状态仓库，用于进程重启后的策略状态恢复。"""

    def __init__(self, path: str | Path = "data/runtime_state/strategies.json"):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = Path(__file__).resolve().parents[2] / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, Dict[str, Any]] = {}
        self._load()

    def get(self, key: str) -> Dict[str, Any] | None:
        state = self._states.get(str(key))
        return dict(state) if isinstance(state, dict) else None

    def set(self, key: str, state: Dict[str, Any]) -> None:
        self._states[str(key)] = self._to_json_safe(dict(state))
        self._save()

    def delete(self, key: str) -> None:
        if str(key) in self._states:
            self._states.pop(str(key), None)
            self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            backup = self.path.with_suffix(self.path.suffix + ".bad")
            try:
                self.path.replace(backup)
            except Exception:
                pass
            self._states = {}
            return
        self._states = payload if isinstance(payload, dict) else {}

    def _save(self) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._states, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

    def _to_json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self._to_json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_json_safe(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
