"""What a core plugin writes to the hub's own data (API 1.3): an event of its own on a project's stream, a value of a
project's signal, a join link of a project, a build of a project's knowledge graph, and notices of its own kinds. Each
write takes the caller's connection and happens in its transaction. Private to ``core_plugin_api``."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, signals, tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.plugin_calls import enabled_in, manifest_of
from evo_agents.hub.server import audit as audit_log
from evo_agents.hub.server import notifications
from evo_agents.hub.server.admin import project_ladder
from evo_agents.hub.server.core_plugin_api._access import ROLES
from evo_agents.hub.server.core_plugin_api._reads import enabled_project
from evo_agents.hub.server.core_plugin_api._types import (
    Invite,
    Plugin,
    Refused,
    SignalPushed,
    own_plugin,
    refusing,
)
from evo_agents.hub.server.invites import DEFAULT_DAYS, JOIN, PAGE, PREFIX, TOKEN_BYTES
from evo_agents.hub.server.security import hash_token
from evo_agents.hub.server.signals import SignalIn

SIGNAL_DAYS = 90  # how long a value pushed stays, as the plugins of evo-agents keep theirs
MAX_INVITE_DAYS = 30
MAX_NOTICE_USERS = 500
NO_ADMIN = "project {project} has no admin to own the ingest key {key}, so the plugin pushes no signal there"
KEY_REVOKED = "an admin of project {project} revoked the ingest key {key} and made no other: the plugin pushes none"
NOT_A_KIND = "{kind!r} is not a notice kind plugin {plugin} contributes ({kinds})"


# Events


async def emit_event(conn: AsyncConnection, plugin: Plugin, project: str, name: str, payload: dict) -> int:
    """Record the plugin's event ``name`` (kind ``plugin.<its id, - as _>.<name>``) on ``project``'s stream, which
    loop rules read, and queue a tick of its Loop when that is on; the event's id."""
    project_id = await enabled_project(conn, plugin, project)
    if not isinstance(payload, dict):
        raise ValueError("an event's payload is a dict")
    return await hub_events.record(conn, hub_events.own_kind(plugin.id, name), project_id, payload)


# Signals


async def _pusher(conn: AsyncConnection, plugin: Plugin, project: str, project_id: int) -> signals.Pusher:
    """The pusher of the ingest key named after the plugin, made on first use with a token nobody reads, owned by the
    project's first admin."""
    k, g = tables.signal_ingest_keys, tables.grants
    rules = await signals.project_rules(conn, project_id)
    found = (
        await conn.execute(
            select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project_id, k.c.name == plugin.id)
        )
    ).all()
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        raise Refused(409, KEY_REVOKED.format(project=project, key=plugin.id))
    if live is None:
        owner = await conn.scalar(
            select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
        )
        if owner is None:
            raise Refused(409, NO_ADMIN.format(project=project, key=plugin.id))
        token = signals.new_ingest_token()  # nobody reads it: the plugin writes in this process
        made = await conn.execute(
            insert(k)
            .values(
                project_id=project_id,
                name=plugin.id,
                max_level=rules.default_label.get("level"),
                token_hash=signals.hash_token(token),
                created_by=owner,
            )
            .returning(k.c.id, k.c.max_level, k.c.revoked_at)
        )
        live = made.one()
    return signals.Pusher(project_id, rules, "ingest", live.id, plugin.id, live.max_level)


async def push_signal(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    key: str,
    value: float | None = None,
    *,
    text: str | None = None,
    measured_at: datetime | None = None,
    tenant: str | None = None,
    external_id: str | None = None,
    days: int = SIGNAL_DAYS,
) -> SignalPushed:
    """Push one value of signal ``key`` (a key an admin of ``project`` declared) as the project's ingest key named
    after the plugin, which it makes on first use; sent again with the same ``external_id`` it is stored once. 409 when
    the project has no admin to own the key, or one revoked it."""
    project_id = await enabled_project(conn, plugin, project)
    with refusing():
        item = SignalIn(
            key=key, value=value, text=text, source=plugin.id, measured_at=measured_at, tenant=tenant,
            external_id=external_id,
        )  # fmt: skip
    pusher = await _pusher(conn, plugin, project, project_id)
    with refusing():
        try:
            done = await signals.push(conn, pusher, [item], days)
        except signals.SignalError as exc:
            raise Refused(exc.status, exc.message) from None
    refused = done.rejected[0]["reason"] if done.rejected else None
    return SignalPushed(done.accepted == 1, done.duplicates == 1, refused)


# Invites


def _url(config: HubConfig | None, token: str) -> str | None:
    return f"{config.public_url}{PAGE}{token}" if config is not None and config.public_url else None


async def create_invite(
    conn: AsyncConnection,
    plugin: Plugin,
    config: HubConfig | None,
    project: str,
    *,
    created_by: int,
    for_login: str | None = None,
    role: str = "reader",
    days: int = DEFAULT_DAYS,
) -> Invite:
    """A join link of ``project`` with ``role`` at the lowest level of its ladder, made by member ``created_by`` (who
    the plugin checked may: an admin of the project, as the hub's route asks), for GitHub login ``for_login`` alone
    when given, valid ``days``; the hub's audit log records it as the member's. The token is in the answer once."""
    project_id = await enabled_project(conn, plugin, project)
    if role not in ROLES:
        raise ValueError(f"a role is one of {', '.join(ROLES)}")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_INVITE_DAYS:
        raise ValueError(f"an invite is valid from 1 to {MAX_INVITE_DAYS} days")
    with refusing():
        _, ladder = await project_ladder(conn, project)
    token = PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    i = tables.invites
    made = (
        await conn.execute(
            insert(i)
            .values(
                kind=JOIN,
                project_id=project_id,
                role=role,
                max_level=ladder[0],
                for_login=for_login,
                token_hash=hash_token(token),
                created_by=created_by,
                expires_at=func.now() + timedelta(days=days),
            )
            .returning(i.c.id, i.c.expires_at)
        )
    ).one()
    target = f"invite:{made.id} kind={JOIN} project={project} role={role} max_level={ladder[0]}"
    if for_login:
        target += f" for={for_login}"
    await audit_log.record(
        conn, actor_id=created_by, token_id=None, action=audit_log.INVITE_CREATE, target=target, project_id=project_id
    )
    return Invite(made.id, token, _url(config, token), made.expires_at)


# The knowledge graph


async def queue_kg_build(conn: AsyncConnection, plugin: Plugin, project: str) -> None:
    """Queue a build of ``project``'s knowledge graph in the caller's transaction, joining one that waits, when the
    project has a knowledge config: what a plugin calls once the data its kg source gives changed."""
    project_id = await enabled_project(conn, plugin, project)
    await queue_if_configured(JobQueue.in_transaction(), conn, project_id, project)


# Notices


async def notify(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    notice_kind: str,
    *,
    users: Iterable[int],
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> tuple[int, ...]:
    """A notice of ``notice_kind``, a kind the plugin's manifest contributes (``notice_kinds``), of ``project`` for
    each member of ``users`` who holds a grant on it, on each of their channels that is on; the ids of the notices, in
    the order of ``users``. Members without a grant are left out."""
    own_plugin(plugin)
    project_id = await enabled_project(conn, plugin, project)
    enabled = await enabled_in(conn, plugin.id, project_id)
    kinds = [item["name"] for item in manifest_of(enabled).contributions("notice_kinds")]
    if notice_kind not in kinds:
        raise ValueError(NOT_A_KIND.format(kind=notice_kind, plugin=plugin.id, kinds=", ".join(kinds) or "none"))
    wanted = list(dict.fromkeys(users))
    if len(wanted) > MAX_NOTICE_USERS:
        raise ValueError(f"a notice goes to at most {MAX_NOTICE_USERS} members at once")
    if link is not None and not (isinstance(link, str) and link.startswith("/") and not link.startswith("//")):
        raise ValueError("a notice's link is a path of the hub's web, such as /p/<project>/runs")
    g = tables.grants
    held = set(
        (await conn.execute(select(g.c.user_id).where(g.c.project_id == project_id, g.c.user_id.in_(wanted)))).scalars()
    )
    found = []
    for user_id in wanted:
        if user_id in held:
            found.append(
                await notifications.notify(
                    conn,
                    user_id=user_id,
                    kind="notice",
                    notice_kind=notice_kind,
                    title=title,
                    body=body,
                    project_id=project_id,
                    link=link,
                )
            )
    return tuple(found)
