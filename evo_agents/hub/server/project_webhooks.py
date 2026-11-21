"""The outbound webhooks of a project, for its admin (``evo_agents.hub.webhook_events``, ``docs/hub.md``, section
"Outbound webhooks").

GET /v1/projects/{p}/webhooks lists them; POST adds one, an https URL and the events it takes, and answers its signing
secret, in that answer alone; PATCH changes its URL, events or switch; POST .../rotate-secret gives it a new secret,
in that answer alone; DELETE removes it with its outbox; GET .../deliveries lists its latest deliveries. Each route is
the admin's of the project: a webhook's URL may hold a receiver's key. The secret is sealed under EVO_HUB_SECRETS_KEY
(``webhook_events.webhook_aad``), and no answer but those two, no log line and no audit row holds it.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, webhook_events
from evo_agents.hub.access import has_role
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit, route_autonomy
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.public_endpoint import host_refusal
from evo_agents.hub.server.sealing import Sealer
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["webhooks"], responses={401: {"model": ErrorBody}})

SECRET_PREFIX = "evx_"  # secret_patterns.HUB_TOKEN masks it in every log line
SECRET_BYTES = 32
MAX_WEBHOOKS = 20  # a project's webhooks at most
MAX_DELIVERIES_SHOWN = 100
NO_STORE = {"Cache-Control": "no-store"}

Event = Literal["run.state", "task.result", "chat.message", "curator.proposal"]
Events = Annotated[list[Event], Field(min_length=1, max_length=len(webhook_events.EVENTS))]
Url = Annotated[str, Field(min_length=9, max_length=webhook_events.MAX_URL_CHARS)]


def _https(url: str) -> str:
    """``url`` when it is https, with a host on the public internet (``public_endpoint.host_refusal``), no user or
    password and no fragment; ValueError otherwise. Its addresses are checked before each try (``webhook_delivery``)."""
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


def _sealer(request: Request) -> Sealer:
    sealer = request.app.state.sealer
    if sealer is None:
        config: HubConfig = request.app.state.config
        missing = ", ".join(config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, f"webhooks need the hub's secrets key to seal their secrets: set {missing}")
    return sealer


def _admin(access: ProjectAccess) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"the webhooks of project {access.name} are its admin's; you hold {held}")


async def _admin_access(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    _admin(access)
    return access


def _listed(project_id: int):
    h, d, u = tables.project_webhooks, tables.webhook_deliveries, tables.users
    pending = func.count(case((d.c.state == "pending", 1)))
    failed = func.count(case((d.c.state == "failed", 1)))
    return (
        select(
            h.c.id,
            h.c.url,
            h.c.events,
            h.c.active,
            u.c.login.label("created_by"),
            h.c.created_at,
            h.c.updated_at,
            pending.label("pending"),
            failed.label("failed"),
        )
        .select_from(h.outerjoin(u, u.c.id == h.c.created_by).outerjoin(d, d.c.webhook_id == h.c.id))
        .where(h.c.project_id == project_id)
        .group_by(h.c.id, u.c.login)
        .order_by(h.c.id)
    )


def _view(row) -> Webhook:
    return Webhook(**{name: getattr(row, name) for name in Webhook.model_fields})


async def _one(conn: AsyncConnection, access: ProjectAccess, webhook_id: int) -> Webhook:
    h = tables.project_webhooks
    row = (await conn.execute(_listed(access.project_id).where(h.c.id == webhook_id))).first()
    if row is None:
        raise HTTPException(404, f"project {access.name} has no webhook {webhook_id}")
    return _view(row)


async def _audit(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, webhook_id: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name} webhook={webhook_id}",
        project_id=access.project_id,
    )


def _secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)


@router.get("/{project}/webhooks", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def list_webhooks(request: Request, project: ProjectName, user: CurrentUser) -> list[Webhook]:
    """The project's outbound webhooks, with how many deliveries of each wait and failed."""
    async with request.app.state.engine.connect() as conn:
        access = await _admin_access(conn, user, project)
        return [_view(row) for row in (await conn.execute(_listed(access.project_id))).all()]


@router.post(
    "/{project}/webhooks",
    status_code=201,
    responses={
        403: {"model": ErrorBody},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        503: {"model": ErrorBody},
    },
    dependencies=[route_autonomy.K8],
)
async def create_webhook(
    request: Request, response: Response, project: ProjectName, body: WebhookCreate, user: CurrentUser
) -> WebhookCreated:
    """Add an outbound webhook; its signing secret is in this answer alone."""
    sealer = _sealer(request)
    h = tables.project_webhooks
    secret = _secret()
    async with request.app.state.engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        held = select(func.count()).select_from(h).where(h.c.project_id == access.project_id)
        if (await conn.execute(held)).scalar_one() >= MAX_WEBHOOKS:
            raise HTTPException(409, f"project {project} has {MAX_WEBHOOKS} webhooks already: delete one first")
        values = {
            "project_id": access.project_id,
            "url": body.url,
            "events": sorted(set(body.events), key=webhook_events.EVENTS.index),
            "active": body.active,
            "created_by": user.user_id,
            "sealed_secret": {},
        }
        webhook_id = (await conn.execute(insert(h).values(**values).returning(h.c.id))).scalar_one()
        sealed = sealer.seal_stored(secret, webhook_events.webhook_aad(webhook_id))
        await conn.execute(update(h).values(sealed_secret=sealed).where(h.c.id == webhook_id))
        await _audit(conn, user, access, audit.WEBHOOK_CREATE, webhook_id)
        made = await _one(conn, access, webhook_id)
    log.info("webhook added", extra={"project": project, "webhook_id": webhook_id})
    response.headers.update(NO_STORE)
    return WebhookCreated(**made.model_dump(), secret=secret)


@router.patch(
    "/{project}/webhooks/{webhook_id}",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def update_webhook(
    request: Request, project: ProjectName, webhook_id: int, body: WebhookUpdate, user: CurrentUser
) -> Webhook:
    """Change a webhook's URL, events or switch; a webhook turned off fails what it left pending."""
    h = tables.project_webhooks
    values: dict = {name: value for name, value in body.model_dump().items() if value is not None}
    if "events" in values:
        values["events"] = sorted(set(values["events"]), key=webhook_events.EVENTS.index)
    async with request.app.state.engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        if values:
            mine = (h.c.id == webhook_id, h.c.project_id == access.project_id)
            await conn.execute(update(h).values(**values, updated_at=func.now()).where(*mine))
            await _audit(conn, user, access, audit.WEBHOOK_UPDATE, webhook_id)
        return await _one(conn, access, webhook_id)


@router.post(
    "/{project}/webhooks/{webhook_id}/rotate-secret",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 503: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def rotate_secret(
    request: Request, response: Response, project: ProjectName, webhook_id: int, user: CurrentUser
) -> WebhookSecret:
    """Give a webhook a new signing secret, in this answer alone; the old one signs nothing from now on."""
    sealer = _sealer(request)
    h = tables.project_webhooks
    secret = _secret()
    async with request.app.state.engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        sealed = sealer.seal_stored(secret, webhook_events.webhook_aad(webhook_id))
        await conn.execute(update(h).values(sealed_secret=sealed, updated_at=func.now()).where(h.c.id == webhook_id))
        await _audit(conn, user, access, audit.WEBHOOK_UPDATE, webhook_id)
    response.headers.update(NO_STORE)
    return WebhookSecret(id=webhook_id, secret=secret)


@router.delete(
    "/{project}/webhooks/{webhook_id}",
    status_code=204,
    response_class=Response,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K8],
)
async def delete_webhook(request: Request, project: ProjectName, webhook_id: int, user: CurrentUser) -> Response:
    """Delete a webhook with its outbox."""
    h = tables.project_webhooks
    async with request.app.state.engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        await conn.execute(delete(h).where(h.c.id == webhook_id, h.c.project_id == access.project_id))
        await _audit(conn, user, access, audit.WEBHOOK_DELETE, webhook_id)
    return Response(status_code=204)


@router.get(
    "/{project}/webhooks/{webhook_id}/deliveries", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
)
async def list_deliveries(
    request: Request, project: ProjectName, webhook_id: int, user: CurrentUser
) -> list[WebhookDelivery]:
    """A webhook's latest deliveries, newest first: their state, tries, last HTTP status and kind of error."""
    d = tables.webhook_deliveries
    async with request.app.state.engine.connect() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        query = (
            select(*(d.c[name] for name in WebhookDelivery.model_fields))
            .where(d.c.webhook_id == webhook_id)
            .order_by(d.c.id.desc())
            .limit(MAX_DELIVERIES_SHOWN)
        )
        rows = (await conn.execute(query)).all()
    return [WebhookDelivery(**row._mapping) for row in rows]
