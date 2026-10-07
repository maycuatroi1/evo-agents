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
EVO_HUB_RUN_LOG_DAYS is how many days the events of a finished run are kept before the daily hub.prune_run_events
deletes them (``evo_agents.hub.server.run_state``). EVO_HUB_RUN_LEASE_SECONDS is how long a claim and each heartbeat
lease a run for (``runs.LEASE_SECONDS`` by default); the reaper finds a run lost once its lease ran out, so tests
shorten it, and it must stay well above the daemon's heartbeat of 15 seconds. EVO_HUB_DECISION_WAIT_SECONDS is how
long a plan run waits for its owner's answer before the reaper (the job hub.recover_runs of ``hub worker``) parks it
(``runs.DECISION_WAIT_SECONDS``, a day, by default); the end-to-end tests shorten it to see a run parked and resumed.

EVO_HUB_FORWARDED_ALLOW_IPS lists the addresses or networks of the reverse proxies whose X-Forwarded-For uvicorn
believes, comma-separated, or ``*``; the client address it yields keys the limit on refused pairing codes. Unset, it
keeps uvicorn's own default (its FORWARDED_ALLOW_IPS variable, else the loopback addresses), so nothing new is
trusted. The session secret also keys the hashes of pairing codes, which therefore need it.

Credentials of runs (``docs/credentials.md``) are optional configuration too. EVO_HUB_SECRETS_KEY is the AES-256-GCM
key that seals them (``evo_agents.hub.server.sealing``), 32 bytes in base64url: without it a route that writes a
secret answers 503 and a run asking for its leases gets none, with the reason. EVO_HUB_GITHUB_APP_ID (the App's ID
or client ID) and EVO_HUB_GITHUB_APP_PRIVATE_KEY (its PEM, where ``\\n`` may stand for each line break, as one line
of an environment file needs) go together or not at all: without them a run gets no GitHub token. The key and the
PEM, its lines included, are registered as secrets, and no error names their value.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from evo_agents.hub.log import dsn_password, register_secret
from evo_agents.hub.runs import DECISION_WAIT_SECONDS, LEASE_SECONDS

# The blob store's bound (evo_agents.hub.blobs takes it from here: that module needs boto3, this one only the stdlib).
DEFAULT_BLOB_CONCURRENCY = 32
MAX_BLOB_CONCURRENCY = 256
# The retention of built graphs (evo_agents.hub.kg_prune): the artifacts of each project's newest graphs that stay.
DEFAULT_KG_KEEP_ARTIFACTS = 3
MAX_KG_KEEP_ARTIFACTS = 1000
# Days the events of a finished run stay (evo_agents.hub.server.run_state.prune_run_events).
DEFAULT_RUN_LOG_DAYS = 30
MAX_RUN_LOG_DAYS = 3650
# Seconds a claim and each heartbeat lease a run for (evo_agents.hub.server.runs).
MIN_RUN_LEASE_SECONDS = 5
MAX_RUN_LEASE_SECONDS = 3600
# Seconds a plan run waits for its owner's answer before the reaper parks it (evo_agents.hub.server.run_state).
MIN_DECISION_WAIT_SECONDS = 1
MAX_DECISION_WAIT_SECONDS = 7 * 86400

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
SECRETS_KEY_BYTES = 32  # the AES-256-GCM key of evo_agents.hub.server.sealing
SECRETS_KEY = re.compile(r"[A-Za-z0-9_-]{43}=?")  # 32 bytes in base64url, padded or not
GITHUB_APP_VARIABLES = ("EVO_HUB_GITHUB_APP_ID", "EVO_HUB_GITHUB_APP_PRIVATE_KEY")
GITHUB_APP_ID = re.compile(r"[0-9]{1,20}|Iv[0-9A-Za-z.]{1,40}")  # the App's ID, or its client ID; JWT's iss takes both
# The PEM GitHub gives an App (PKCS#1), or the same key as PKCS#8; the second group is its base64.
PRIVATE_KEY_PEM = re.compile(
    r"-----BEGIN ((?:RSA )?)PRIVATE KEY-----\n([A-Za-z0-9+/=\n]+)\n-----END \1PRIVATE KEY-----"
)


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
    session_secret: str | None = None  # signs the web login cookie, keys the CSRF tokens and the pairing code hashes
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
    run_log_days: int = DEFAULT_RUN_LOG_DAYS  # days a finished run's events are kept
    run_lease_seconds: int = LEASE_SECONDS  # how long a claim and each heartbeat lease a run for
    decision_wait_seconds: int = DECISION_WAIT_SECONDS  # how long a plan run waits for an answer before it parks
    forwarded_allow_ips: str | None = None  # proxies whose X-Forwarded-For uvicorn believes; None: uvicorn's default
    secrets_key: bytes | None = None  # seals the credentials of runs; never logged, returned or stored
    github_app_id: str | None = None  # the GitHub App that makes the runs' tokens: its ID or client ID
    github_app_private_key: str | None = None  # its PEM, with real line breaks; never logged or returned

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

    def credentials_missing(self) -> list[str]:
        """The variable the hub needs to keep secrets and lease credentials; empty when it can."""
        return [] if self.secrets_key else ["EVO_HUB_SECRETS_KEY"]

    def github_app_missing(self) -> list[str]:
        """The EVO_HUB_GITHUB_APP_* variables the runs' GitHub tokens still need; empty when the App is configured."""
        values = (self.github_app_id, self.github_app_private_key)
        return [name for name, value in zip(GITHUB_APP_VARIABLES, values, strict=True) if not value]


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


def _run_log_days(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_RUN_LOG_DAYS"
    value = _number(env, name, DEFAULT_RUN_LOG_DAYS, minimum=1)
    if value > MAX_RUN_LOG_DAYS:
        raise ConfigError(name, f"{name} must be at most {MAX_RUN_LOG_DAYS}, got {value}")
    return value


def _run_lease_seconds(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_RUN_LEASE_SECONDS"
    value = _number(env, name, LEASE_SECONDS, minimum=MIN_RUN_LEASE_SECONDS)
    if value > MAX_RUN_LEASE_SECONDS:
        raise ConfigError(name, f"{name} must be at most {MAX_RUN_LEASE_SECONDS}, got {value}")
    return value


def _decision_wait_seconds(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_DECISION_WAIT_SECONDS"
    value = _number(env, name, DECISION_WAIT_SECONDS, minimum=MIN_DECISION_WAIT_SECONDS)
    if value > MAX_DECISION_WAIT_SECONDS:
        raise ConfigError(name, f"{name} must be at most {MAX_DECISION_WAIT_SECONDS}, got {value}")
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


def _forwarded_allow_ips(env: Mapping[str, str]) -> str | None:
    """EVO_HUB_FORWARDED_ALLOW_IPS as uvicorn takes it: ``*``, or IP addresses and networks, comma-separated. uvicorn
    reads anything else as a literal that never matches a TCP peer, so a typo would trust nobody without a word."""
    name = "EVO_HUB_FORWARDED_ALLOW_IPS"
    raw = _text(env, name)
    if raw is None:
        return None
    expected = f"{name} must be *, or IP addresses and networks such as 10.0.0.0/8, comma-separated"
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    if not entries:
        raise ConfigError(name, expected)
    if entries == ["*"]:
        return "*"
    for entry in entries:
        parse = ipaddress.ip_network if "/" in entry else ipaddress.ip_address  # as uvicorn tells them apart
        try:
            parse(entry)
        except ValueError:
            raise ConfigError(name, f"{expected}, got {entry!r}") from None
    return ",".join(entries)


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


def _secrets_key(env: Mapping[str, str]) -> bytes | None:
    """EVO_HUB_SECRETS_KEY as its 32 bytes, registered as a secret before anything can fail; no error shows it."""
    name = "EVO_HUB_SECRETS_KEY"
    raw = _secret(env, name)
    if raw is None:
        return None
    if not SECRETS_KEY.fullmatch(raw):
        raise ConfigError(
            name,
            f"{name} must be {SECRETS_KEY_BYTES} bytes in base64url (43 characters of A-Z, a-z, 0-9, - and _), such as "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))' prints",
        )
    key = base64.urlsafe_b64decode(raw.rstrip("=") + "=")
    register_secret(base64.b64encode(key).decode())  # the same key in standard base64, as a tool may print it
    return key


def _github_app(env: Mapping[str, str]) -> dict:
    """The GitHub App's ID and private key, both or neither; the PEM and each of its lines are registered as secrets."""
    id_name, key_name = GITHUB_APP_VARIABLES
    app_id = _text(env, id_name)
    raw = _secret(env, key_name)
    pem = None
    if raw is not None:
        pem = raw.replace("\\n", "\n").replace("\r\n", "\n").strip()
        match = PRIVATE_KEY_PEM.fullmatch(pem)
        if not match:
            raise ConfigError(
                key_name,
                f"{key_name} must be the App's private key, the whole .pem file GitHub gives, with line breaks or \\n "
                "between its lines",
            )
        register_secret(pem)
        for line in match.group(2).split("\n"):
            if len(line) >= 16:  # a line of base64 shows up alone in a repr or a partial dump
                register_secret(line)
        pem += "\n"
    if app_id is not None and not GITHUB_APP_ID.fullmatch(app_id):
        raise ConfigError(id_name, f"{id_name} must be the GitHub App's ID or its client ID (Iv...), got {app_id!r}")
    missing = [name for name, value in ((id_name, app_id), (key_name, pem)) if not value]
    if len(missing) == 1:
        raise ConfigError(
            missing[0], f"{missing[0]} is not set: the GitHub App needs {id_name} and {key_name} together, or neither"
        )
    return {"github_app_id": app_id, "github_app_private_key": pem}


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
        run_log_days=_run_log_days(env),
        run_lease_seconds=_run_lease_seconds(env),
        decision_wait_seconds=_decision_wait_seconds(env),
        forwarded_allow_ips=_forwarded_allow_ips(env),
        **_blob_store(env),
        secrets_key=_secrets_key(env),
        **_github_app(env),
    )
