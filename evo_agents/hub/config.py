"""Hub server configuration, read from ``EVO_HUB_*`` variables; a command line flag wins over its variable.

A missing required variable or a malformed value raises ``ConfigError`` naming the variable, and the command
stops before anything starts. Loading a configuration registers its secrets with ``log`` so no log line can
carry them. Standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub.log import dsn_password, register_secret

DEFAULT_DATA_DIR = "~/.evo/hub-server/cache"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    def __init__(self, variable: str, message: str):
        super().__init__(message)
        self.variable = variable


@dataclass(frozen=True)
class HubConfig:
    dsn: str
    data_dir: Path  # a cache: everything in it can be rebuilt from Postgres and the blob store
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    sentry_dsn: str | None = None
    pool_min_size: int = 1
    pool_max_size: int = 10
    pool_timeout: float = 10.0  # seconds to wait for a pooled connection before failing the request

    def __repr__(self) -> str:  # the DSNs hold credentials; keep them out of tracebacks and debug output
        return f"HubConfig(data_dir={str(self.data_dir)!r}, host={self.host!r}, port={self.port})"


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _number(env: Mapping[str, str], name: str, default, kind=int, minimum=None):
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = kind(raw)
    except ValueError:
        what = "an integer" if kind is int else "a number"
        raise ConfigError(name, f"{name} must be {what}, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(name, f"{name} must be at least {minimum}, got {raw!r}")
    return value


def load_log_level(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    level = (_text(env, "EVO_HUB_LOG_LEVEL") or "INFO").upper()
    if level not in LOG_LEVELS:
        raise ConfigError("EVO_HUB_LOG_LEVEL", f"EVO_HUB_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    return level


def load_dsn(env: Mapping[str, str] | None = None, dsn: str | None = None) -> str:
    """The Postgres DSN from ``dsn`` (the --dsn flag) or EVO_HUB_DSN, its password registered as a secret."""
    env = os.environ if env is None else env
    value = (dsn or "").strip() or _text(env, "EVO_HUB_DSN")
    if not value:
        raise ConfigError(
            "EVO_HUB_DSN", "EVO_HUB_DSN is not set: set it to the postgresql:// URI of the hub database, or pass --dsn"
        )
    register_secret(dsn_password(value))
    register_secret(_text(env, "PGPASSWORD"))  # libpq falls back to it when the DSN has no password
    if "://" in value:
        scheme = value.split("://", 1)[0]
        if scheme not in ("postgresql", "postgres"):
            raise ConfigError("EVO_HUB_DSN", f"EVO_HUB_DSN must be a postgresql:// URI, not {scheme}://")
    elif "=" not in value:
        raise ConfigError("EVO_HUB_DSN", "EVO_HUB_DSN is neither a postgresql:// URI nor libpq key=value pairs")
    return value


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    dsn: str | None = None,
    data_dir: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> HubConfig:
    """The server configuration; keyword arguments are command line flags and win over the variables."""
    env = os.environ if env is None else env
    dsn = load_dsn(env, dsn)
    sentry_dsn = _text(env, "EVO_HUB_SENTRY_DSN")
    register_secret(sentry_dsn)
    port = port if port is not None else _number(env, "EVO_HUB_PORT", DEFAULT_PORT)
    if not 1 <= port <= 65535:
        raise ConfigError("EVO_HUB_PORT", f"EVO_HUB_PORT (or --port) must be between 1 and 65535, got {port}")
    pool_min = _number(env, "EVO_HUB_POOL_MIN_SIZE", 1, minimum=0)
    pool_max = _number(env, "EVO_HUB_POOL_MAX_SIZE", 10, minimum=1)
    if pool_max < pool_min:
        raise ConfigError(
            "EVO_HUB_POOL_MAX_SIZE", f"EVO_HUB_POOL_MAX_SIZE ({pool_max}) is below the minimum ({pool_min})"
        )
    return HubConfig(
        dsn=dsn,
        data_dir=Path(data_dir or _text(env, "EVO_HUB_DATA_DIR") or DEFAULT_DATA_DIR).expanduser(),
        host=host or _text(env, "EVO_HUB_HOST") or DEFAULT_HOST,
        port=port,
        sentry_dsn=sentry_dsn,
        pool_min_size=pool_min,
        pool_max_size=pool_max,
        pool_timeout=_number(env, "EVO_HUB_POOL_TIMEOUT", 10.0, kind=float, minimum=0.1),
    )
