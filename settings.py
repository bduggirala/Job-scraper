"""Configuration loading for the company ATS scraper.

Loads ``config/settings.yaml`` once and exposes it as a dict-like object with
dotted-path lookup. All relative paths in the config resolve against the
project root (the directory containing this file).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

_env_loaded = False


def load_env_file(path: str | Path | None = None, *, force: bool = False) -> dict[str, str]:
    """Read ``.env`` into the process environment, once per process.

    The credentials and the digest recipient are read from the environment
    rather than from ``config/settings.yaml``, because that file is in git and
    an SMTP password must never be able to land in it. ``.env`` is the
    gitignored companion that holds the real values - but nothing was reading
    it, so every variable ``.env.example`` documents had to be exported by hand
    before it took effect. A recipient sitting unread in ``.env`` beside a
    placeholder in the tracked config is the failure this prevents.

    **A variable already set in the environment always wins.** ``.env`` is a
    convenience for local runs; an operator who exported something, or a
    scheduler that set it on the job, has been more deliberate than a file on
    disk and must not be overridden by one.

    Deliberately hand-rolled rather than adding ``python-dotenv``: the format
    that matters here is ``KEY=value`` with ``#`` comments, and a dependency
    for that is not worth the supply chain.

    Args:
        path: the file to read. Defaults to ``.env`` beside this module.
        force: re-read even if a previous call already loaded one.

    Returns:
        ``{key: value}`` for the variables this call actually set. Empty when
        there is no file, or when every key was already present.
    """
    global _env_loaded
    if _env_loaded and not force:
        return {}

    env_path = Path(path) if path else DEFAULT_ENV_PATH
    if not env_path.is_absolute():
        env_path = PROJECT_ROOT / env_path

    _env_loaded = True
    if not env_path.exists():
        return {}

    applied: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:  # unreadable .env must never stop a run
        return {}

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # "export FOO=bar" is how the same file gets sourced by a shell.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
            value = value[1:-1]
        if key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value

    return applied


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
    """Load (and memoize) the YAML settings file.

    Also loads ``.env`` on the first call, so every entry point gets the same
    environment without each one having to remember to ask for it.
    """
    load_env_file()
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
