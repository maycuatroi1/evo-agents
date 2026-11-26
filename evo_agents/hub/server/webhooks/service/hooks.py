"""What a project's admin does with its outbound webhooks: list them, add one (its signing secret answered in that
answer alone), change its URL, events or switch, give it a new secret, delete it with its outbox, and list its latest
deliveries. Each is the admin's of the project: a webhook's URL may hold a receiver's key. The secret is sealed under
EVO_HUB_SECRETS_KEY (``webhook_events.webhook_aad``), and no answer but the two that make one, no log line and no
audit row holds it.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import webhook_events
from evo_agents.hub.access import has_role
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.sealing import Sealer
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.webhooks import repository
from evo_agents.hub.server.webhooks.models import (
    MAX_WEBHOOKS,
    SECRET_BYTES,
    SECRET_PREFIX,
    Webhook,
    WebhookCreate,
    WebhookCreated,
    WebhookDelivery,
    WebhookSecret,
    WebhookUpdate,
)

log = logging.getLogger(__name__)


def _sealer(sealer: Sealer | None, config: HubConfig) -> Sealer:
    if sealer is None:
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


def _view(row) -> Webhook:
    return Webhook(**{name: getattr(row, name) for name in Webhook.model_fields})


async def _one(conn: AsyncConnection, access: ProjectAccess, webhook_id: int) -> Webhook:
    row = await repository.webhook_of(conn, access.project_id, webhook_id)
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


async def list_webhooks(engine, user: Principal, project: str) -> list[Webhook]:
    """Project ``project``'s webhooks, with how many deliveries of each wait and failed."""
    async with engine.connect() as conn:
        access = await _admin_access(conn, user, project)
        return [_view(row) for row in await repository.webhooks_of(conn, access.project_id)]


async def create_webhook(
    engine, sealer: Sealer | None, config: HubConfig, user: Principal, project: str, body: WebhookCreate
) -> WebhookCreated:
    """Add the webhook ``body`` to project ``project``; its signing secret is in this answer alone."""
    sealer = _sealer(sealer, config)
    secret = _secret()
    async with engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        if await repository.count(conn, access.project_id) >= MAX_WEBHOOKS:
            raise HTTPException(409, f"project {project} has {MAX_WEBHOOKS} webhooks already: delete one first")
        values = {
            "project_id": access.project_id,
            "url": body.url,
            "events": sorted(set(body.events), key=webhook_events.EVENTS.index),
            "active": body.active,
            "created_by": user.user_id,
            "sealed_secret": {},
        }
        webhook_id = await repository.add(conn, values)
        sealed = sealer.seal_stored(secret, webhook_events.webhook_aad(webhook_id))
        await repository.seal(conn, webhook_id, sealed)
        await _audit(conn, user, access, audit.WEBHOOK_CREATE, webhook_id)
        made = await _one(conn, access, webhook_id)
    log.info("webhook added", extra={"project": project, "webhook_id": webhook_id})
    return WebhookCreated(**made.model_dump(), secret=secret)


async def update_webhook(engine, user: Principal, project: str, webhook_id: int, body: WebhookUpdate) -> Webhook:
    """Change webhook ``webhook_id`` of project ``project`` as ``body`` says."""
    values: dict = {name: value for name, value in body.model_dump().items() if value is not None}
    if "events" in values:
        values["events"] = sorted(set(values["events"]), key=webhook_events.EVENTS.index)
    async with engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        if values:
            await repository.change(conn, access.project_id, webhook_id, values)
            await _audit(conn, user, access, audit.WEBHOOK_UPDATE, webhook_id)
        return await _one(conn, access, webhook_id)


async def rotate_secret(
    engine, sealer: Sealer | None, config: HubConfig, user: Principal, project: str, webhook_id: int
) -> WebhookSecret:
    """Give webhook ``webhook_id`` of project ``project`` a new signing secret, in this answer alone."""
    sealer = _sealer(sealer, config)
    secret = _secret()
    async with engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        sealed = sealer.seal_stored(secret, webhook_events.webhook_aad(webhook_id))
        await repository.seal(conn, webhook_id, sealed, touch=True)
        await _audit(conn, user, access, audit.WEBHOOK_UPDATE, webhook_id)
    return WebhookSecret(id=webhook_id, secret=secret)


async def delete_webhook(engine, user: Principal, project: str, webhook_id: int) -> None:
    """Delete webhook ``webhook_id`` of project ``project`` with its outbox."""
    async with engine.begin() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        await repository.remove(conn, access.project_id, webhook_id)
        await _audit(conn, user, access, audit.WEBHOOK_DELETE, webhook_id)


async def list_deliveries(engine, user: Principal, project: str, webhook_id: int) -> list[WebhookDelivery]:
    """Webhook ``webhook_id``'s latest deliveries, newest first."""
    async with engine.connect() as conn:
        access = await _admin_access(conn, user, project)
        await _one(conn, access, webhook_id)
        rows = await repository.deliveries(conn, webhook_id)
    return [WebhookDelivery(**row._mapping) for row in rows]
