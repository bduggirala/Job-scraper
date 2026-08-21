"""Configuration loading for the company ATS scraper.

Loads ``config/settings.yaml`` once and exposes it as a dict-like object with
dotted-path lookup. All relative paths in the config resolve against the
project root (the directory containing this file).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class Settings:
    """Thin wrapper around the YAML config with dotted-key access."""

    def __init__(self, data: dict, path: Path):
        self._data = data
        self.path = path

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def __getitem__(self, dotted_key: str) -> Any:
        value = self.get(dotted_key, _MISSING)
        if value is _MISSING:
            raise KeyError(dotted_key)
        return value

    def resolve_path(self, dotted_key: str, default: str | None = None) -> Path:
        """Return a config value as an absolute path rooted at PROJECT_ROOT."""
        raw = self.get(dotted_key, default)
        if raw is None:
            raise KeyError(f"No path configured for {dotted_key!r}")
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate)

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)


_MISSING = object()
_cache: dict[Path, Settings] = {}


def load_settings(path: str | Path | None = None) -> Settings:
    """Load (and memoize) the YAML settings file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    config_path = config_path if config_path.is_absolute() else (PROJECT_ROOT / config_path)

    if config_path in _cache:
        return _cache[config_path]

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    settings = Settings(data, config_path)
    _cache[config_path] = settings
    return settings
