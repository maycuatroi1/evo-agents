"""Reading one variable of the hub's environment for ``evo_agents.hub.config``: its text, a number, an http(s) base
URL or a secret, each refused with a ``ConfigError`` that names the variable. ``config`` imports these back under the
names it has always used, and re-exports ``ConfigError``, so its callers do not change."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from evo_agents.hub.log import register_secret


class ConfigError(ValueError):
    def __init__(self, variable: str, message: str):
        super().__init__(message)
        self.variable = variable


def text(env: Mapping[str, str], name: str) -> str | None:
    """The variable's value without the spaces around it; None when it is unset or blank."""
    value = env.get(name, "").strip()
    return value or None


def number(env: Mapping[str, str], name: str, default, kind=int, minimum=None):
    """The variable as ``kind`` (int by default), ``default`` when unset, at least ``minimum`` when one is given."""
    raw = text(env, name)
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


def url(env: Mapping[str, str], name: str, default: str | None) -> str | None:
    """An http(s) base URL without a trailing slash, query or fragment."""
    raw = text(env, name)
    if raw is None:
        return default
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError(name, f"{name} must be an http(s) URL such as https://example.org, got {raw!r}")
    return raw.rstrip("/")


def secret(env: Mapping[str, str], name: str, minimum: int = 1) -> str | None:
    """The variable as a secret the log never prints, at least ``minimum`` characters long."""
    value = text(env, name)
    register_secret(value)
    if value is not None and len(value) < minimum:
        raise ConfigError(name, f"{name} must be at least {minimum} characters long")
    return value


__all__ = ["ConfigError", "number", "secret", "text", "url"]
