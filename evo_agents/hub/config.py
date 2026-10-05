"""Hub server configuration, read from ``EVO_HUB_*`` variables; a command line flag wins over its variable.

A missing required variable or a malformed value raises ``ConfigError`` naming the variable, and the command
stops before anything starts. Loading a configuration registers its secrets with ``log`` so no log line can
carry them. Standard library only.

Sign-in is optional configuration: without EVO_HUB_GITHUB_CLIENT_ID nobody can sign in, and without the client
secret, the session secret or the public URL the web sign-in answers 503 while the CLI's device flow still works.
The GitHub URLs are configurable so tests and Playwright can point the hub at a fake GitHub.

The blob store (Cloudflare R2, or any S3 API) is configured by the four EVO_HUB_S3_* variables together: none of
them leaves it unconfigured, so the blob routes answer 503 and ``hub worker`` refuses to start; some but not all of
them is a ConfigError naming the first one missing. The key pair is registered as secrets like the DSN password.
EVO_HUB_BLOB_CONCURRENCY caps the uploads one process checks and copies in the store at once, every commit together
(``evo_agents.hub.blobs``). EVO_HUB_KG_KEEP_ARTIFACTS is how many of each project's newest built graphs keep their
artifact in the bucket (``evo_agents.hub.kg_prune``); at least 1, since the api reads the newest.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from evo_agents.hub.log import dsn_password, register_secret

# The blob store's bound (evo_agents.hub.blobs takes it from here: that module needs boto3, this one only the stdlib).
DEFAULT_BLOB_CONCURRENCY = 32
MAX_BLOB_CONCURRENCY = 256
# The retention of built graphs (evo_agents.hub.kg_prune): the artifacts of each project's newest graphs that stay.
DEFAULT_KG_KEEP_ARTIFACTS = 3
MAX_KG_KEEP_ARTIFACTS = 1000

DEFAULT_DATA_DIR = "~/.evo/hub-server/cache"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_GITHUB_URL = "https://github.com"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_TIMEOUT = 10.0  # seconds for one call to GitHub, connecting included
MIN_SESSION_SECRET = 32  # characters; the secret keys HMAC-SHA256
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")  # a GitHub login, as the users table accepts it
S3_VARIABLES = ("EVO_HUB_S3_ENDPOINT", "EVO_HUB_S3_BUCKET", "EVO_HUB_S3_ACCESS_KEY_ID", "EVO_HUB_S3_SECRET_ACCESS_KEY")
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")  # an S3 bucket name, as R2 accepts it


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
    admins: frozenset[str] = frozenset()  # lowercased GitHub logins from EVO_HUB_ADMINS
    github_client_id: str | None = None  # public: the CLI asks for it to start the device flow
    github_client_secret: str | None = None  # the web flow's code exchange; never logged or returned
    session_secret: str | None = None  # signs the web login cookie and keys the CSRF tokens
    public_url: str | None = None  # where browsers reach the hub, without a trailing slash
    github_url: str = DEFAULT_GITHUB_URL
    github_api_url: str = DEFAULT_GITHUB_API_URL
    github_timeout: float = DEFAULT_GITHUB_TIMEOUT
    s3_endpoint: str | None = None  # the account's S3 API, https://<account id>.r2.cloudflarestorage.com for R2
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None  # never logged
    s3_secret_access_key: str | None = None  # never logged or returned
    blob_concurrency: int = DEFAULT_BLOB_CONCURRENCY  # uploads one process seals or publishes at once
    kg_keep_artifacts: int = DEFAULT_KG_KEEP_ARTIFACTS  # newest graphs per project whose artifact the retention keeps

    def __repr__(self) -> str:  # the DSNs and secrets are credentials; keep them out of tracebacks and debug output
        return f"HubConfig(data_dir={str(self.data_dir)!r}, host={self.host!r}, port={self.port})"

    def is_admin(self, login: str) -> bool:
        return login.lower() in self.admins

    def web_login_missing(self) -> list[str]:
        """The variables the web sign-in still needs; empty when it can run."""
        needed = {
            "EVO_HUB_GITHUB_CLIENT_ID": self.github_client_id,
            "EVO_HUB_GITHUB_CLIENT_SECRET": self.github_client_secret,
            "EVO_HUB_SESSION_SECRET": self.session_secret,
            "EVO_HUB_PUBLIC_URL": self.public_url,
        }
        return [name for name, value in needed.items() if not value]

    def blob_store_missing(self) -> list[str]:
        """The EVO_HUB_S3_* variables the blob store still needs; empty when it is configured."""
        values = (self.s3_endpoint, self.s3_bucket, self.s3_access_key_id, self.s3_secret_access_key)
        return [name for name, value in zip(S3_VARIABLES, values, strict=True) if not value]


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


def _blob_concurrency(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_BLOB_CONCURRENCY"
    value = _number(env, name, DEFAULT_BLOB_CONCURRENCY, minimum=1)
    if value > MAX_BLOB_CONCURRENCY:
        raise ConfigError(name, f"{name} must be at most {MAX_BLOB_CONCURRENCY}, got {value}")
    return value


def _kg_keep_artifacts(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_KG_KEEP_ARTIFACTS"
    value = _number(env, name, DEFAULT_KG_KEEP_ARTIFACTS, minimum=1)
    if value > MAX_KG_KEEP_ARTIFACTS:
        raise ConfigError(name, f"{name} must be at most {MAX_KG_KEEP_ARTIFACTS}, got {value}")
    return value


def _url(env: Mapping[str, str], name: str, default: str | None) -> str | None:
    """An http(s) base URL without a trailing slash, query or fragment."""
    raw = _text(env, name)
    if raw is None:
        return default
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise ConfigError(name, f"{name} must be an http(s) URL such as https://example.org, got {raw!r}")
    return raw.rstrip("/")


def _admins(env: Mapping[str, str]) -> frozenset[str]:
    logins = [part.strip() for part in (_text(env, "EVO_HUB_ADMINS") or "").split(",") if part.strip()]
    for login in logins:
        if not LOGIN.fullmatch(login):
            raise ConfigError("EVO_HUB_ADMINS", f"EVO_HUB_ADMINS must list GitHub logins, got {login!r}")
    return frozenset(login.lower() for login in logins)


def _secret(env: Mapping[str, str], name: str, minimum: int = 1) -> str | None:
    value = _text(env, name)
    register_secret(value)
    if value is not None and len(value) < minimum:
        raise ConfigError(name, f"{name} must be at least {minimum} characters long")
    return value


def _blob_store(env: Mapping[str, str]) -> dict:
    """The S3 settings, all four or none; the key pair is registered as secrets before anything can fail."""
    endpoint_name, bucket_name, key_name, secret_name = S3_VARIABLES
    key_id, secret = _secret(env, key_name), _secret(env, secret_name)
    values = {
        "s3_endpoint": _url(env, endpoint_name, None),
        "s3_bucket": _text(env, bucket_name),
        "s3_access_key_id": key_id,
        "s3_secret_access_key": secret,
    }
    missing = [name for name, value in zip(S3_VARIABLES, values.values(), strict=True) if not value]
    if missing and len(missing) < len(S3_VARIABLES):
        raise ConfigError(
            missing[0],
            f"{missing[0]} is not set: the blob store needs {', '.join(S3_VARIABLES)} together, or none of them",
        )
    if values["s3_bucket"] and not BUCKET.fullmatch(values["s3_bucket"]):
        raise ConfigError(bucket_name, f"{bucket_name} must be an S3 bucket name, got {values['s3_bucket']!r}")
    return values


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
        admins=_admins(env),
        github_client_id=_text(env, "EVO_HUB_GITHUB_CLIENT_ID"),
        github_client_secret=_secret(env, "EVO_HUB_GITHUB_CLIENT_SECRET"),
        session_secret=_secret(env, "EVO_HUB_SESSION_SECRET", MIN_SESSION_SECRET),
        public_url=_url(env, "EVO_HUB_PUBLIC_URL", None),
        github_url=_url(env, "EVO_HUB_GITHUB_URL", DEFAULT_GITHUB_URL),
        github_api_url=_url(env, "EVO_HUB_GITHUB_API_URL", DEFAULT_GITHUB_API_URL),
        github_timeout=_number(env, "EVO_HUB_GITHUB_TIMEOUT", DEFAULT_GITHUB_TIMEOUT, kind=float, minimum=0.1),
        blob_concurrency=_blob_concurrency(env),
        kg_keep_artifacts=_kg_keep_artifacts(env),
        **_blob_store(env),
    )
