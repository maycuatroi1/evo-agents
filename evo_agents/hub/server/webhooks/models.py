"""The models of outbound webhooks: the limits of a project's webhooks and of a pass of the job, the events a
webhook takes, and the API's shapes. A webhook's URL is https, with a host on the public internet
(``public_endpoint.host_refusal``), no user or password and no fragment; its addresses are checked again before each
try (``service.delivery``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import webhook_events
from evo_agents.hub.server.public_endpoint import host_refusal

SECRET_PREFIX = "evx_"  # secret_patterns.HUB_TOKEN masks it in every log line
SECRET_BYTES = 32
MAX_WEBHOOKS = 20  # a project's webhooks at most
MAX_DELIVERIES_SHOWN = 100
NO_STORE = {"Cache-Control": "no-store"}

TIMEOUT = 10.0  # a try, from resolving the host to the end of the answer
CLAIM_SECONDS = TIMEOUT + 60  # a claimed delivery is not due again for this long, unless its try is written first
PASS_SECONDS = 40.0  # a pass starts no try after this
BATCH = 200  # a pass tries this many at most

Event = Literal["run.state", "task.result", "chat.message", "curator.proposal"]
Events = Annotated[list[Event], Field(min_length=1, max_length=len(webhook_events.EVENTS))]
Url = Annotated[str, Field(min_length=9, max_length=webhook_events.MAX_URL_CHARS)]


def _https(url: str) -> str:
    """``url`` when it is https, with a host on the public internet (``public_endpoint.host_refusal``), no user or
    password and no fragment; ValueError otherwise. Its addresses are checked before each try (``service.delivery``)."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 (a port that is not a number raises here)
    except ValueError:
        raise ValueError("a webhook's URL is https://<host>/<path>") from None
    if parts.scheme != "https" or not parts.hostname or parts.fragment:
        raise ValueError("a webhook's URL is https://<host>/<path>, with no fragment")
    if parts.username is not None or parts.password is not None:
        raise ValueError("a webhook's URL holds no user or password: put a receiver's key in its path or query")
    refusal = host_refusal(parts.hostname.lower().rstrip("."))
    if refusal is not None:
        raise ValueError(refusal)
    return url


class WebhookCreate(BaseModel):
    url: Url
    events: Events
    active: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _https(value)


class WebhookUpdate(BaseModel):
    url: Url | None = None
    events: Events | None = None
    active: bool | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return None if value is None else _https(value)


class Webhook(BaseModel):
    id: int
    url: str
    events: list[Event]
    active: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    pending: int
    failed: int


class WebhookCreated(Webhook):
    secret: str  # in this answer alone: the hub keeps it sealed


class WebhookSecret(BaseModel):
    id: int
    secret: str


class WebhookDelivery(BaseModel):
    id: int
    event: Event
    state: Literal["pending", "delivered", "failed"]
    attempts: int
    last_status: int | None
    last_error: str | None
    next_at: datetime
    created_at: datetime
    delivered_at: datetime | None
