"""The named sections of the hub's configuration (``evo_agents.hub.config.HubConfig``): the database, the server, the
web sign-in, the blob store, the knowledge graph, retention, runs, credentials, the GitHub Apps, Telegram, push,
plugins, the model gateway, the seller and the event bus. Each field of HubConfig belongs to exactly one section,
whose class names its fields as HubConfig does, says which variables it still needs (``missing``) and is read off a
configuration with ``of``: ``config.telegram.missing()``. HubConfig keeps its fields flat, so a configuration is still
made and replaced field by field. Standard library only."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar, overload

if TYPE_CHECKING:
    from pathlib import Path

    from evo_agents.plugins.signing import Publisher

PLUGIN_HOST_VARIABLES = ("EVO_HUB_PLUGIN_HOST_URL", "EVO_HUB_PLUGIN_HOST_TOKEN")
GATEWAY_VARIABLES = ("EVO_HUB_GATEWAY_URL", "EVO_HUB_GATEWAY_SECRET")
S3_VARIABLES = ("EVO_HUB_S3_ENDPOINT", "EVO_HUB_S3_BUCKET", "EVO_HUB_S3_ACCESS_KEY_ID", "EVO_HUB_S3_SECRET_ACCESS_KEY")
GITHUB_APP_VARIABLES = ("EVO_HUB_GITHUB_APP_ID", "EVO_HUB_GITHUB_APP_PRIVATE_KEY")
CURATOR_APP_VARIABLES = ("EVO_HUB_CURATOR_APP_ID", "EVO_HUB_CURATOR_APP_PRIVATE_KEY")  # the Curator's own App
TELEGRAM_VARIABLES = ("EVO_HUB_TELEGRAM_BOT_TOKEN", "EVO_HUB_TELEGRAM_WEBHOOK_SECRET")
VAPID_VARIABLES = ("EVO_HUB_VAPID_PRIVATE_KEY", "EVO_HUB_VAPID_SUBJECT")
APNS_VARIABLES = ("EVO_HUB_APNS_KEY_ID", "EVO_HUB_APNS_TEAM_ID", "EVO_HUB_APNS_PRIVATE_KEY")
FCM_VARIABLE = "EVO_HUB_FCM_SERVICE_ACCOUNT"


def unset(variables: tuple[str, ...], values: tuple[Any, ...]) -> list[str]:
    """The names among ``variables`` whose value is empty."""
    return [name for name, value in zip(variables, values, strict=True) if not value]


@dataclass(frozen=True)
class Section:
    """A part of the configuration: a subclass's fields are HubConfig's of the same names."""

    title: ClassVar[str]

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(field.name for field in fields(cls))

    @classmethod
    def of(cls, config: object) -> Self:
        """This section of ``config``."""
        return cls(**{name: getattr(config, name) for name in cls.names()})

    def missing(self) -> list[str]:
        """The variables this section still needs; empty when what it configures can run, or needs none."""
        return []


@dataclass(frozen=True)
class Database(Section):
    title = "database"
    dsn: str
    pool_min_size: int
    pool_max_size: int
    pool_timeout: float  # seconds to wait for a pooled connection before failing the request
    heartbeat_pool_size: int  # connections of the api's own pool for the workers' heartbeats and claims


@dataclass(frozen=True)
class Server(Section):
    title = "server"
    data_dir: Path  # a cache: everything in it can be rebuilt from Postgres and the blob store
    host: str
    port: int
    sentry_dsn: str | None
    admins: frozenset[str]  # lowercased GitHub logins from EVO_HUB_ADMINS
    web_time_zone: str  # an IANA name: members' notification settings until they choose one
    forwarded_allow_ips: str | None  # proxies whose X-Forwarded-For uvicorn believes; None: uvicorn's default


@dataclass(frozen=True)
class WebLogin(Section):
    title = "web sign-in"
    github_client_id: str | None
    github_client_secret: str | None
    session_secret: str | None
    public_url: str | None
    github_url: str
    github_api_url: str
    github_timeout: float

    def missing(self) -> list[str]:
        """The variables the web sign-in still needs; empty when it can run."""
        variables = ("EVO_HUB_GITHUB_CLIENT_ID", "EVO_HUB_GITHUB_CLIENT_SECRET", "EVO_HUB_SESSION_SECRET")
        values = (self.github_client_id, self.github_client_secret, self.session_secret, self.public_url)
        return unset((*variables, "EVO_HUB_PUBLIC_URL"), values)


@dataclass(frozen=True)
class BlobStoreSection(Section):
    title = "blob store"
    s3_endpoint: str | None
    s3_bucket: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    s3_internal_endpoint: str | None
    s3_provider: str
    s3_region: str
    s3_addressing_style: str
    blob_concurrency: int

    def missing(self) -> list[str]:
        """The EVO_HUB_S3_* variables the blob store still needs; empty when it is configured."""
        values = (self.s3_endpoint, self.s3_bucket, self.s3_access_key_id, self.s3_secret_access_key)
        return unset(S3_VARIABLES, values)


@dataclass(frozen=True)
class KnowledgeGraph(Section):
    title = "knowledge graph"
    kg_keep_artifacts: int  # newest graphs per project whose artifact the retention keeps
    kg_upload_max_bytes: int  # the largest archive `hub kg upload` takes


@dataclass(frozen=True)
class Retention(Section):
    title = "retention"
    run_log_days: int
    audit_days: int
    transcript_days: int
    signal_days: int


@dataclass(frozen=True)
class Runs(Section):
    title = "runs"
    run_lease_seconds: int
    decision_wait_seconds: int


@dataclass(frozen=True)
class Credentials(Section):
    title = "credentials"
    secrets_key: bytes | None

    def missing(self) -> list[str]:
        """The variable the hub needs to keep secrets and lease credentials; empty when it can."""
        return [] if self.secrets_key else ["EVO_HUB_SECRETS_KEY"]


@dataclass(frozen=True)
class GitHubApps(Section):
    title = "GitHub Apps"
    github_app_id: str | None
    github_app_private_key: str | None
    curator_app_id: str | None
    curator_app_private_key: str | None

    def missing(self) -> list[str]:
        """The EVO_HUB_GITHUB_APP_* variables the runs' GitHub tokens still need; empty when the App is configured."""
        return unset(GITHUB_APP_VARIABLES, (self.github_app_id, self.github_app_private_key))

    def curator_missing(self) -> list[str]:
        """The EVO_HUB_CURATOR_APP_* variables the Curator's runs still need; empty when its App is configured."""
        return unset(CURATOR_APP_VARIABLES, (self.curator_app_id, self.curator_app_private_key))


@dataclass(frozen=True)
class Telegram(Section):
    title = "Telegram"
    telegram_bot_token: str | None
    telegram_webhook_secret: str | None
    telegram_api_url: str

    def missing(self) -> list[str]:
        """The EVO_HUB_TELEGRAM_* variables the Telegram channel still needs; empty when it is on."""
        return unset(TELEGRAM_VARIABLES, (self.telegram_bot_token, self.telegram_webhook_secret))


@dataclass(frozen=True)
class Push(Section):
    title = "push"
    vapid_private_key: str | None
    vapid_subject: str | None  # None: the public URL
    apns_key_id: str | None
    apns_team_id: str | None
    apns_private_key: str | None
    fcm_project_id: str | None
    fcm_client_email: str | None
    fcm_private_key: str | None

    def webpush_missing(self, subject: str | None) -> list[str]:
        """The variables Web Push still needs, the key and a ``subject``; empty when it is on."""
        missing = [] if self.vapid_private_key else [VAPID_VARIABLES[0]]
        return missing + ([] if subject else [VAPID_VARIABLES[1]])

    def apns_missing(self) -> list[str]:
        """The EVO_HUB_APNS_* variables APNs still needs; empty when the hub sends to iPhones."""
        return unset(APNS_VARIABLES, (self.apns_key_id, self.apns_team_id, self.apns_private_key))

    def fcm_missing(self) -> list[str]:
        """The variable FCM still needs; empty when the hub sends to Android phones."""
        return [] if self.fcm_private_key else [FCM_VARIABLE]


@dataclass(frozen=True)
class Plugins(Section):
    title = "plugins"
    plugin_publishers: tuple[Publisher, ...]
    plugin_core_publishers: frozenset[str]
    plugin_publishers_locked: bool
    market_default_source: str | None
    plugin_host_url: str | None
    plugin_host_token: str | None

    def missing(self) -> list[str]:
        """The EVO_HUB_PLUGIN_HOST_* variables the backends of isolated plugins still need; empty when they run."""
        return unset(PLUGIN_HOST_VARIABLES, (self.plugin_host_url, self.plugin_host_token))


@dataclass(frozen=True)
class Gateway(Section):
    title = "model gateway"
    gateway_url: str | None
    gateway_secret: str | None

    def missing(self) -> list[str]:
        """The variable the model gateway still needs; empty when it is on."""
        return [] if self.gateway_secret else [GATEWAY_VARIABLES[1]]


@dataclass(frozen=True)
class Seller(Section):
    title = "seller"
    seller_id: str | None
    seller_key: bytes | None
    seller_payment_instructions: str | None
    seller_signal_project: str | None


@dataclass(frozen=True)
class Bus(Section):
    title = "event bus and cache"
    broker_url: str | None
    redis_url: str | None


S = TypeVar("S", bound=Section)


class Part(Generic[S]):
    """A section as an attribute of HubConfig: ``config.telegram`` is ``Telegram.of(config)``. Not a dataclass field,
    having no annotation."""

    def __init__(self, section: type[S]) -> None:
        self.section = section

    @overload
    def __get__(self, config: None, owner: type) -> Self: ...

    @overload
    def __get__(self, config: object, owner: type | None = None) -> S: ...

    def __get__(self, config: object, owner: type | None = None) -> S | Self:
        return self if config is None else self.section.of(config)


SECTIONS: tuple[type[Section], ...] = (
    Database,
    Server,
    WebLogin,
    BlobStoreSection,
    KnowledgeGraph,
    Retention,
    Runs,
    Credentials,
    GitHubApps,
    Telegram,
    Push,
    Plugins,
    Gateway,
    Seller,
    Bus,
)
