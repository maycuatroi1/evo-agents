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
EVO_HUB_S3_INTERNAL_ENDPOINT, optional, is where the api and the worker reach the same store themselves, such as the
store's address on the platform's network when EVO_HUB_S3_ENDPOINT is a public hostname behind a proxy that caps a
request's body; the presigned URLs handed to clients keep EVO_HUB_S3_ENDPOINT. Set without the four, it is a
ConfigError.
EVO_HUB_BLOB_CONCURRENCY caps the uploads one process checks and copies in the store at once, every commit together
(``evo_agents.hub.blobs``). EVO_HUB_KG_KEEP_ARTIFACTS is how many of each project's newest built graphs keep their
artifact in the bucket (``evo_agents.hub.kg_prune``); at least 1, since the api reads the newest.
EVO_HUB_RUN_LOG_DAYS is how many days the events of a finished run are kept before the daily hub.prune_run_events
deletes them (``evo_agents.hub.server.run_state``). EVO_HUB_AUDIT_DAYS is how many days an audit row is kept before
the daily hub.prune_audit deletes it (``evo_agents.hub.worker``): 365 by default, from 30 to 3650, so a month of the
trail always stays. EVO_HUB_TRANSCRIPT_DAYS is how many days the hub keeps a session's transcript after its last part
came before the daily hub.prune_transcripts deletes its content (``evo_agents.hub.server.transcripts``): 365 by
default, from 30 to 3650. EVO_HUB_SIGNAL_DAYS is how many days the hub keeps a signal after it was measured before
the daily hub.prune_signals deletes it (``evo_agents.hub.signals``): 400 by default, from 30 to 3650.
EVO_HUB_RUN_LEASE_SECONDS is how long a claim and each heartbeat lease a run for (``runs.LEASE_SECONDS`` by default);
the reaper finds a run lost once its lease ran out, so tests shorten it, and it must stay well above the daemon's
heartbeat of 15 seconds. EVO_HUB_DECISION_WAIT_SECONDS is how long a plan run waits
for its owner's answer before the reaper (the job hub.recover_runs of ``hub worker``) parks it
(``runs.DECISION_WAIT_SECONDS``, a day, by default); the end-to-end tests shorten it to see a run parked and resumed.

deploy/hub/docker-compose.yml passes the api and the worker every variable read here except those the image sets
(EVO_HUB_HOST, EVO_HUB_PORT, EVO_HUB_DATA_DIR) and those only tests point elsewhere (the GitHub and Telegram URLs and
EVO_HUB_GITHUB_TIMEOUT); tests/hub/test_compose.py fails, naming the lines to add, when a new one is missing there or
in deploy/hub/.env.example.

EVO_HUB_FORWARDED_ALLOW_IPS lists the addresses or networks of the reverse proxies whose X-Forwarded-For uvicorn
believes, comma-separated, or ``*``; the client address it yields keys the limit on refused pairing codes. Unset, it
keeps uvicorn's own default (its FORWARDED_ALLOW_IPS variable, else the loopback addresses), so nothing new is
trusted. The session secret also keys the hashes of pairing codes, which therefore need it, and the split of the
Curator's replay suites (``evo_agents.hub.replay.split_of``), which no hub builds without it.

Credentials of runs (``docs/credentials.md``) are optional configuration too. EVO_HUB_SECRETS_KEY is the AES-256-GCM
key that seals them (``evo_agents.hub.server.sealing``), 32 bytes in base64url: without it a route that writes a
secret answers 503 and a run asking for its leases gets none, with the reason. EVO_HUB_GITHUB_APP_ID (the App's ID
or client ID) and EVO_HUB_GITHUB_APP_PRIVATE_KEY (its PEM, where ``\\n`` may stand for each line break, as one line
of an environment file needs) go together or not at all: without them a run gets no GitHub token. The key and the
PEM, its lines included, are registered as secrets, and no error names their value. EVO_HUB_CURATOR_APP_ID and
EVO_HUB_CURATOR_APP_PRIVATE_KEY are the second App, evo-agents-curator, read the same way: the runs of the Curator get
their tokens from it alone (``evo_agents.hub.judge``), and without it they get none, while every other run keeps the
first App's.

The Telegram channel (the core plugin telegram of evo-agents-core-plugins) is optional too. EVO_HUB_TELEGRAM_BOT_TOKEN
is the token of the hub's one bot, as BotFather gives it, and EVO_HUB_TELEGRAM_WEBHOOK_SECRET the secret_token the hub
sets with setWebhook and checks on every update (1 to 256 characters of A-Z, a-z, 0-9, _ and -). Both are registered as
secrets. Without either the channel is off: linking answers 503, the webhook 404, a delivery to Telegram fails at once,
and the hub runs on. EVO_HUB_TELEGRAM_API_URL is where the Bot API is (https://api.telegram.org), so tests can point the
hub at a fake one.

Web Push (``evo_agents.hub.server.webpush``) is optional too. EVO_HUB_VAPID_PRIVATE_KEY is the hub's VAPID key, the
private scalar of a P-256 key in base64url (32 bytes, 43 characters), registered as a secret; the public key browsers
subscribe with is derived from it. EVO_HUB_VAPID_SUBJECT is where a push service may reach the hub's operator, a
mailto: or https: URL, EVO_HUB_PUBLIC_URL when unset. Without the key, or without a subject and a public URL, Web Push
is off: GET /v1/push/config says so, a delivery to a browser fails at once, and the rest of the outbox runs as before.
Pushes to the hub's apps (``evo_agents.hub.server.push_native``) are optional the same way, each sender on its own.
APNs takes EVO_HUB_APNS_KEY_ID and EVO_HUB_APNS_TEAM_ID (10 characters of A-Z and 0-9 each, as Apple shows them) and
EVO_HUB_APNS_PRIVATE_KEY, the .p8 file of that key (a P-256 key in PEM, where ``\\n`` may stand for each line break),
registered as a secret; a device names its app's topic and its environment itself. FCM takes
EVO_HUB_FCM_SERVICE_ACCOUNT, the JSON key file of a Google service account that may send Firebase Cloud Messaging, on
one line or in base64, registered as a secret with its private key. Without all of a sender's variables that sender
is off, as Web Push is without its key; a malformed one is a ConfigError naming it, never showing it.

Plugins (``evo_agents.plugins``) are optional too. EVO_HUB_PLUGIN_PUBLISHERS lists the publishers whose signed bundles
the hub accepts, comma-separated ``ID=sigstore:OWNER/REPO/.github/workflows/FILE.yml`` (a GitHub Actions workflow that
signs keyless with Sigstore) or ``ID=ed25519:KEY`` (a public key, 32 bytes in base64) entries, several for one id when
it has several workflows or keys (``evo_agents.plugins.signing.parse_publishers``). Unset, the hub trusts nobody and
refuses every bundle. EVO_HUB_PLUGIN_CORE_PUBLISHERS lists, comma-separated, the ids among them whose plugins may be
core plugins, with Python in the hub's processes; each must be in EVO_HUB_PLUGIN_PUBLISHERS.

The backends of isolated plugins run in the plugin host, ``evo-agents hub plugin-host``, a service of its own that
holds none of the hub's variables (docs/plugins.md). EVO_HUB_PLUGIN_HOST_URL is where the api reaches it (on the
compose network, http://evo-agents-hub-plugins:8090) and EVO_HUB_PLUGIN_HOST_TOKEN the secret the two share, at least
32 characters and not the value an example once showed, registered as a secret: the host sends it to the api's
/internal/plugin-host/ routes, which the reverse proxy does not forward, and the api to the host. Without both the api
runs no backend: those routes answer 503, and so does a call to a plugin.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from evo_agents.hub.log import dsn_password, register_secret
from evo_agents.hub.runs import DECISION_WAIT_SECONDS, LEASE_SECONDS
from evo_agents.plugins.host import MIN_HOST_TOKEN, host_token_problem
from evo_agents.plugins.signing import PUBLISHER_ID, Publisher, parse_publishers

# The blob store's bound (evo_agents.hub.blobs takes it from here: that module needs boto3, this one only the stdlib).
DEFAULT_BLOB_CONCURRENCY = 32
MAX_BLOB_CONCURRENCY = 256
# The retention of built graphs (evo_agents.hub.kg_prune): the artifacts of each project's newest graphs that stay.
DEFAULT_KG_KEEP_ARTIFACTS = 3
MAX_KG_KEEP_ARTIFACTS = 1000
# Days the events of a finished run stay (evo_agents.hub.server.run_state.prune_run_events).
DEFAULT_RUN_LOG_DAYS = 30
MAX_RUN_LOG_DAYS = 3650
# Days an audit row stays (evo_agents.hub.worker.prune_audit).
DEFAULT_AUDIT_DAYS = 365
MIN_AUDIT_DAYS = 30
MAX_AUDIT_DAYS = 3650
# Days a session's transcript stays after its last part came (evo_agents.hub.server.transcripts.prune_transcripts).
DEFAULT_TRANSCRIPT_DAYS = 365
MIN_TRANSCRIPT_DAYS = 30
MAX_TRANSCRIPT_DAYS = 3650
# Days a signal stays after it was measured (evo_agents.hub.signals.prune_signals).
DEFAULT_SIGNAL_DAYS = 400
MIN_SIGNAL_DAYS = 30
MAX_SIGNAL_DAYS = 3650
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
MIN_PLUGIN_HOST_TOKEN = MIN_HOST_TOKEN  # characters of the secret the api and the plugin host share
PLUGIN_HOST_VARIABLES = ("EVO_HUB_PLUGIN_HOST_URL", "EVO_HUB_PLUGIN_HOST_TOKEN")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")  # a GitHub login, as the users table accepts it
S3_VARIABLES = ("EVO_HUB_S3_ENDPOINT", "EVO_HUB_S3_BUCKET", "EVO_HUB_S3_ACCESS_KEY_ID", "EVO_HUB_S3_SECRET_ACCESS_KEY")
S3_INTERNAL_ENDPOINT = "EVO_HUB_S3_INTERNAL_ENDPOINT"  # optional: where the hub itself reaches the store
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")  # an S3 bucket name, as R2 accepts it
SECRETS_KEY_BYTES = 32  # the AES-256-GCM key of evo_agents.hub.server.sealing
SECRETS_KEY = re.compile(r"[A-Za-z0-9_-]{43}=?")  # 32 bytes in base64url, padded or not
GITHUB_APP_VARIABLES = ("EVO_HUB_GITHUB_APP_ID", "EVO_HUB_GITHUB_APP_PRIVATE_KEY")
GITHUB_APP_ID = re.compile(r"[0-9]{1,20}|Iv[0-9A-Za-z.]{1,40}")  # the App's ID, or its client ID; JWT's iss takes both
CURATOR_APP_VARIABLES = ("EVO_HUB_CURATOR_APP_ID", "EVO_HUB_CURATOR_APP_PRIVATE_KEY")  # the Curator's own App
TELEGRAM_VARIABLES = ("EVO_HUB_TELEGRAM_BOT_TOKEN", "EVO_HUB_TELEGRAM_WEBHOOK_SECRET")
DEFAULT_TELEGRAM_API_URL = "https://api.telegram.org"
TELEGRAM_BOT_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}")  # <bot id>:<secret>, as BotFather gives it
TELEGRAM_WEBHOOK_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")  # what setWebhook takes as secret_token
VAPID_VARIABLES = ("EVO_HUB_VAPID_PRIVATE_KEY", "EVO_HUB_VAPID_SUBJECT")
VAPID_KEY = re.compile(r"[A-Za-z0-9_-]{43}=?")  # the private scalar of a P-256 key: 32 bytes in base64url
# The order of P-256's group: a private scalar lies in [1, n - 1] (SEC 2, section 2.4.2).
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
VAPID_SUBJECT = re.compile(r"mailto:[^\s@]+@[^\s@]+|https://[^\s]+")  # what RFC 8292's "sub" may be
APNS_VARIABLES = ("EVO_HUB_APNS_KEY_ID", "EVO_HUB_APNS_TEAM_ID", "EVO_HUB_APNS_PRIVATE_KEY")
APPLE_ID = re.compile(r"[A-Z0-9]{10}")  # a key ID or a team ID, as the Apple Developer account shows it
# The DER of an elliptic-curve key on P-256 names both: id-ecPublicKey and prime256v1 (RFC 5480, section 2.1.1).
EC_KEY_OID, P256_OID = bytes.fromhex("06072a8648ce3d0201"), bytes.fromhex("06082a8648ce3d030107")
RSA_KEY_OID = bytes.fromhex("06092a864886f70d010101")  # rsaEncryption (RFC 8017, appendix C)
FCM_VARIABLE = "EVO_HUB_FCM_SERVICE_ACCOUNT"
GOOGLE_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
SERVICE_ACCOUNT_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
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
    s3_internal_endpoint: str | None = None  # where the api and worker call the store; None: s3_endpoint
    blob_concurrency: int = DEFAULT_BLOB_CONCURRENCY  # uploads one process seals or publishes at once
    kg_keep_artifacts: int = DEFAULT_KG_KEEP_ARTIFACTS  # newest graphs per project whose artifact the retention keeps
    run_log_days: int = DEFAULT_RUN_LOG_DAYS  # days a finished run's events are kept
    audit_days: int = DEFAULT_AUDIT_DAYS  # days an audit row is kept
    transcript_days: int = DEFAULT_TRANSCRIPT_DAYS  # days a transcript is kept after its last part came
    signal_days: int = DEFAULT_SIGNAL_DAYS  # days a signal is kept after it was measured
    run_lease_seconds: int = LEASE_SECONDS  # how long a claim and each heartbeat lease a run for
    decision_wait_seconds: int = DECISION_WAIT_SECONDS  # how long a plan run waits for an answer before it parks
    forwarded_allow_ips: str | None = None  # proxies whose X-Forwarded-For uvicorn believes; None: uvicorn's default
    secrets_key: bytes | None = None  # seals the credentials of runs; never logged, returned or stored
    github_app_id: str | None = None  # the GitHub App that makes the runs' tokens: its ID or client ID
    github_app_private_key: str | None = None  # its PEM, with real line breaks; never logged or returned
    curator_app_id: str | None = None  # the Curator's App, evo-agents-curator: its runs' tokens come from it alone
    curator_app_private_key: str | None = None  # its PEM, with real line breaks; never logged or returned
    telegram_bot_token: str | None = None  # the hub's bot; never logged, returned or stored
    telegram_webhook_secret: str | None = None  # the secret_token of its webhook; never logged or returned
    telegram_api_url: str = DEFAULT_TELEGRAM_API_URL
    vapid_private_key: str | None = None  # the hub's VAPID key in base64url; never logged or returned
    vapid_subject: str | None = None  # where push services may reach the operator; None: the public URL
    apns_key_id: str | None = None  # the APNs key the hub signs its provider tokens with
    apns_team_id: str | None = None  # the Apple Developer team that key belongs to
    apns_private_key: str | None = None  # that key's .p8, PEM with real line breaks; never logged or returned
    fcm_project_id: str | None = None  # the Firebase project of the hub's service account
    fcm_client_email: str | None = None  # the service account
    fcm_private_key: str | None = None  # its private key, PEM; never logged or returned
    plugin_publishers: tuple[Publisher, ...] = ()  # whose signed plugin bundles the hub accepts
    plugin_core_publishers: frozenset[str] = frozenset()  # the ids among them whose plugins may be core plugins
    plugin_host_url: str | None = None  # where the api reaches the plugin host
    plugin_host_token: str | None = None  # the secret the api and the plugin host share; never logged or returned

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

    def plugin_host_missing(self) -> list[str]:
        """The EVO_HUB_PLUGIN_HOST_* variables the backends of isolated plugins still need; empty when they run."""
        values = (self.plugin_host_url, self.plugin_host_token)
        return [name for name, value in zip(PLUGIN_HOST_VARIABLES, values, strict=True) if not value]

    def telegram_missing(self) -> list[str]:
        """The EVO_HUB_TELEGRAM_* variables the Telegram channel still needs; empty when it is on."""
        values = (self.telegram_bot_token, self.telegram_webhook_secret)
        return [name for name, value in zip(TELEGRAM_VARIABLES, values, strict=True) if not value]

    @property
    def push_subject(self) -> str | None:
        """The subject of the hub's VAPID JWTs: EVO_HUB_VAPID_SUBJECT, else the public URL."""
        return self.vapid_subject or self.public_url

    def webpush_missing(self) -> list[str]:
        """The variables Web Push still needs, the key and a subject (or the public URL); empty when it is on."""
        missing = [] if self.vapid_private_key else [VAPID_VARIABLES[0]]
        return missing + ([] if self.push_subject else [VAPID_VARIABLES[1]])

    def apns_missing(self) -> list[str]:
        """The EVO_HUB_APNS_* variables APNs still needs; empty when the hub sends to iPhones."""
        values = (self.apns_key_id, self.apns_team_id, self.apns_private_key)
        return [name for name, value in zip(APNS_VARIABLES, values, strict=True) if not value]

    def fcm_missing(self) -> list[str]:
        """The variable FCM still needs; empty when the hub sends to Android phones."""
        return [] if self.fcm_private_key else [FCM_VARIABLE]

    def github_app_missing(self) -> list[str]:
        """The EVO_HUB_GITHUB_APP_* variables the runs' GitHub tokens still need; empty when the App is configured."""
        values = (self.github_app_id, self.github_app_private_key)
        return [name for name, value in zip(GITHUB_APP_VARIABLES, values, strict=True) if not value]

    def curator_app_missing(self) -> list[str]:
        """The EVO_HUB_CURATOR_APP_* variables the Curator's runs still need; empty when its App is configured."""
        values = (self.curator_app_id, self.curator_app_private_key)
        return [name for name, value in zip(CURATOR_APP_VARIABLES, values, strict=True) if not value]


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


def _audit_days(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_AUDIT_DAYS"
    value = _number(env, name, DEFAULT_AUDIT_DAYS, minimum=MIN_AUDIT_DAYS)
    if value > MAX_AUDIT_DAYS:
        raise ConfigError(name, f"{name} must be at most {MAX_AUDIT_DAYS}, got {value}")
    return value


def _transcript_days(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_TRANSCRIPT_DAYS"
    value = _number(env, name, DEFAULT_TRANSCRIPT_DAYS, minimum=MIN_TRANSCRIPT_DAYS)
    if value > MAX_TRANSCRIPT_DAYS:
        raise ConfigError(name, f"{name} must be at most {MAX_TRANSCRIPT_DAYS}, got {value}")
    return value


def _signal_days(env: Mapping[str, str]) -> int:
    name = "EVO_HUB_SIGNAL_DAYS"
    value = _number(env, name, DEFAULT_SIGNAL_DAYS, minimum=MIN_SIGNAL_DAYS)
    if value > MAX_SIGNAL_DAYS:
        raise ConfigError(name, f"{name} must be at most {MAX_SIGNAL_DAYS}, got {value}")
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
    """The S3 settings, all four or none, and the optional internal endpoint; the key pair is registered as secrets
    before anything can fail."""
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
    internal = _url(env, S3_INTERNAL_ENDPOINT, None)
    if internal and missing:
        raise ConfigError(
            S3_INTERNAL_ENDPOINT,
            f"{S3_INTERNAL_ENDPOINT} is set but the blob store is not: set {', '.join(S3_VARIABLES)} too, or unset it",
        )
    return {**values, "s3_internal_endpoint": internal}


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


def _github_app(env: Mapping[str, str], variables: tuple[str, str] = GITHUB_APP_VARIABLES) -> tuple:
    """A GitHub App's ID and private key, both or neither, from ``variables``; the PEM and each of its lines are
    registered as secrets."""
    id_name, key_name = variables
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
    return app_id, pem


def _apps(env: Mapping[str, str]) -> dict:
    """The two GitHub Apps: the workers' (EVO_HUB_GITHUB_APP_*) and the Curator's (EVO_HUB_CURATOR_APP_*)."""
    app_id, pem = _github_app(env)
    curator_id, curator_pem = _github_app(env, CURATOR_APP_VARIABLES)
    return {
        "github_app_id": app_id,
        "github_app_private_key": pem,
        "curator_app_id": curator_id,
        "curator_app_private_key": curator_pem,
    }


def _telegram(env: Mapping[str, str]) -> dict:
    """The bot's token and the webhook's secret, each registered as a secret before anything can fail; either may be
    missing, which turns the channel off. No error shows a value."""
    token_name, secret_name = TELEGRAM_VARIABLES
    token, secret = _secret(env, token_name), _secret(env, secret_name)
    if token is not None and not TELEGRAM_BOT_TOKEN.fullmatch(token):
        raise ConfigError(token_name, f"{token_name} must be the bot's token as BotFather gives it, <digits>:<secret>")
    if secret is not None and not TELEGRAM_WEBHOOK_SECRET.fullmatch(secret):
        raise ConfigError(secret_name, f"{secret_name} must be 1 to 256 characters of A-Z, a-z, 0-9, _ and -")
    if token is not None:
        register_secret(token.partition(":")[2])  # the part after the bot's id, as a tool may print it alone
    return {
        "telegram_bot_token": token,
        "telegram_webhook_secret": secret,
        "telegram_api_url": _url(env, "EVO_HUB_TELEGRAM_API_URL", DEFAULT_TELEGRAM_API_URL),
    }


def _vapid(env: Mapping[str, str]) -> dict:
    """The VAPID key, registered as a secret before anything can fail, and the subject; no error shows the key."""
    key_name, subject_name = VAPID_VARIABLES
    key = _secret(env, key_name)
    if key is not None:
        raw = base64.urlsafe_b64decode(key.rstrip("=") + "=") if VAPID_KEY.fullmatch(key) else b""
        if not 0 < int.from_bytes(raw, "big") < P256_ORDER:
            raise ConfigError(
                key_name,
                f"{key_name} must be the private key of a P-256 key pair, 32 bytes in base64url (43 characters of A-Z, "
                "a-z, 0-9, - and _), such as `npx web-push generate-vapid-keys` prints",
            )
        register_secret(base64.b64encode(raw).decode())  # the same key in standard base64, as a tool may print it
    subject = _text(env, subject_name)
    if subject is not None and not VAPID_SUBJECT.fullmatch(subject):
        raise ConfigError(subject_name, f"{subject_name} must be a mailto: or https: URL, got {subject!r}")
    return {"vapid_private_key": key, "vapid_subject": subject}


def _pkcs8(pem: str) -> tuple[bytes, str] | None:
    """The DER of the PKCS#8 key ``pem`` (with real line breaks), as Apple's .p8 file and a Google service account's
    private_key hold one, with the key's canonical PEM; each are registered as secrets, the lines of its base64 too.
    None when ``pem`` is no such key."""
    match = PRIVATE_KEY_PEM.fullmatch(pem.replace("\r\n", "\n").strip())
    if not match or match.group(1):  # PKCS#1 names its algorithm in the label; PKCS#8 names none
        return None
    lines = match.group(2).split("\n")
    try:
        der = base64.b64decode("".join(lines), validate=True)
    except ValueError:
        return None
    canonical = match.group(0) + "\n"
    for value in (pem, canonical, *[line for line in lines if len(line) >= 16]):
        register_secret(value)  # a line of base64 shows up alone in a repr or a partial dump
    return der, canonical


def _apns(env: Mapping[str, str]) -> dict:
    """The APNs key's ID, its team and its .p8, the key registered as a secret before anything can fail; any of them
    missing turns APNs off. No error shows the key."""
    id_name, team_name, key_name = APNS_VARIABLES
    raw = _secret(env, key_name)
    pem = None
    if raw is not None:
        found = _pkcs8(raw.replace("\\n", "\n"))
        if found is None or EC_KEY_OID not in found[0] or P256_OID not in found[0]:
            raise ConfigError(
                key_name,
                f"{key_name} must be the .p8 file of an APNs key, a P-256 key in PEM, with line breaks or \\n between "
                "its lines",
            )
        pem = found[1]
    found_ids = {name: _text(env, name) for name in (id_name, team_name)}
    for name, value in found_ids.items():
        if value is not None and not APPLE_ID.fullmatch(value):
            raise ConfigError(
                name, f"{name} must be 10 characters of A-Z and 0-9, as the Apple Developer account shows it"
            )
    return {"apns_key_id": found_ids[id_name], "apns_team_id": found_ids[team_name], "apns_private_key": pem}


def _service_account(raw: str) -> dict | None:
    """The JSON object of a service account's key file, given as JSON or as base64 of it; None for anything else."""
    text = raw
    if not raw.startswith("{"):
        try:
            text = base64.b64decode(raw, validate=True).decode()
        except ValueError:  # not base64, or not UTF-8 underneath
            return None
        register_secret(text)
    try:
        account = json.loads(text)
    except ValueError:
        return None
    return account if isinstance(account, dict) else None


def _fcm(env: Mapping[str, str]) -> dict:
    """The project, the email and the private key of the service account in EVO_HUB_FCM_SERVICE_ACCOUNT, registered
    as secrets with the key before anything can fail; unset turns FCM off. No error shows the key."""
    name = FCM_VARIABLE
    raw = _secret(env, name)
    if raw is None:
        return {"fcm_project_id": None, "fcm_client_email": None, "fcm_private_key": None}
    account = _service_account(raw)
    if account is None:
        raise ConfigError(
            name, f"{name} must be the JSON key file of a Google service account, on one line or in base64"
        )
    key = account.get("private_key")
    found = _pkcs8(key) if isinstance(key, str) else None
    project, email = account.get("project_id"), account.get("client_email")
    for field, good in (
        ("type", account.get("type") == "service_account"),
        ("private_key", found is not None and RSA_KEY_OID in found[0]),
        ("project_id", isinstance(project, str) and GOOGLE_PROJECT_ID.fullmatch(project) is not None),
        ("client_email", isinstance(email, str) and SERVICE_ACCOUNT_EMAIL.fullmatch(email) is not None),
    ):
        if not good:
            raise ConfigError(
                name,
                f"{name} must be the JSON key file of a Google service account: its {field} is missing or not one",
            )
    return {"fcm_project_id": project, "fcm_client_email": email, "fcm_private_key": found[1]}


def _plugins(env: Mapping[str, str]) -> dict:
    """The publishers of plugins the hub trusts, and the ids among them whose plugins may be core plugins."""
    name, core_name = "EVO_HUB_PLUGIN_PUBLISHERS", "EVO_HUB_PLUGIN_CORE_PUBLISHERS"
    try:
        publishers = parse_publishers(_text(env, name))
    except ValueError as exc:
        raise ConfigError(name, str(exc)) from None
    core = [part.strip() for part in (_text(env, core_name) or "").split(",") if part.strip()]
    known = {publisher.id for publisher in publishers}
    for publisher_id in core:
        if not PUBLISHER_ID.fullmatch(publisher_id):
            raise ConfigError(core_name, f"{core_name} lists publisher ids, such as acme, not {publisher_id!r}")
        if publisher_id not in known:
            raise ConfigError(
                core_name, f"{core_name} names {publisher_id}, which {name} does not list: a core publisher signs too"
            )
    url_name, token_name = PLUGIN_HOST_VARIABLES
    token = _secret(env, token_name, MIN_PLUGIN_HOST_TOKEN)
    problem = host_token_problem(token) if token else None
    if problem:
        raise ConfigError(token_name, f"{token_name} {problem}")
    return {
        "plugin_publishers": publishers,
        "plugin_core_publishers": frozenset(core),
        "plugin_host_url": _url(env, url_name, None),
        "plugin_host_token": token,
    }


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
        audit_days=_audit_days(env),
        transcript_days=_transcript_days(env),
        signal_days=_signal_days(env),
        run_lease_seconds=_run_lease_seconds(env),
        decision_wait_seconds=_decision_wait_seconds(env),
        forwarded_allow_ips=_forwarded_allow_ips(env),
        **_blob_store(env),
        secrets_key=_secrets_key(env),
        **_apps(env),
        **_telegram(env),
        **_vapid(env),
        **_apns(env),
        **_fcm(env),
        **_plugins(env),
    )
