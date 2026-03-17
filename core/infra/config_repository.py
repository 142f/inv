"""Infrastructure adapter for strategy config loading."""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from core.config import ConfigLoader


class ConfigRepository:
    def __init__(self, config_path: Path | None = None):
        self._loader = ConfigLoader(config_path=config_path)

    @property
    def config_path(self) -> Path:
        return self._loader.config_path

    def load_if_changed(self) -> Tuple[bool, List[dict]]:
        return self._loader.load_if_changed()

    def force_load(self) -> List[dict]:
        return self._loader.force_load()

