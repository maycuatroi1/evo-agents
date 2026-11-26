"""What a member reads of their own notifications: the list (open decisions and proposals first, then newest first;
filtered by unread, kind, project, run and words of the title or body, a page at a time), the count (the unread ones,
and the decisions and the proposals still waiting for an answer, for the bell, which every page of the web asks every
10 seconds: the hub's cache keeps it COUNT_TTL seconds and drops it when a transaction that wrote a notification, a
decision, a proposal or a grant commits, ``cache.watch``), and the mark of a read (by ids, or all), audited as
notification.read with the ids, never the text. A notification of a project the member no longer holds a grant on is
not shown. Answering a decision reads its notification too (``decisions``). A notification of kind proposal is a tier
2 proposal of the Curator in the member's Inbox (``proposals``), and answering it reads it too."""

from __future__ import annotations

import logging

from evo_agents.hub import cache, tables
from evo_agents.hub.server import audit, ops_views, output_views
from evo_agents.hub.server.notifications.models import (
    COUNT_TTL,
    NotificationCount,
    NotificationList,
    ReadRequest,
    ReadResult,
)
from evo_agents.hub.server.notifications.repository import listing
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

cache.watch("notifications", tables.notifications, tables.decisions, tables.proposals, tables.grants)


async def notification_list(
    engine,
    user_id: int,
    *,
    unread: bool,
    kind: str | None,
    project: str | None,
    run: int | None,
    q: str | None,
    handler: str | None,
    limit: int,
    offset: int,
) -> NotificationList:
    """Member ``user_id``'s notifications that match the filters, a page of them, with their files and their holder
    in the ops queue."""
    found = listing.listed(user_id, unread, kind, project, run, (q or "").strip() or None, handler)
    async with engine.begin() as conn:
        page, matching = await listing.page(conn, found, limit, offset)
        carried = await output_views.by_notification(conn, page)
        holding = await ops_views.by_notification(conn, [item.id for item in page])
    page = [item.model_copy(update={"outputs": carried[item.id]}) if item.id in carried else item for item in page]
    page = [item.model_copy(update={"ops": holding[item.id]}) if item.id in holding else item for item in page]
    return NotificationList(notifications=page, total=matching, limit=limit, offset=offset)


async def notification_count(engine, hub_cache, user_id: int) -> NotificationCount:
    """How many of member ``user_id``'s notifications are unread, and how many decisions and proposals wait for
    their answer, through ``hub_cache``."""

    async def load() -> dict:
        async with engine.begin() as conn:
            return dict((await conn.execute(listing.counts(user_id))).one()._mapping)

    key = f"notifications:count:{user_id}"
    return NotificationCount(**await hub_cache.fetch(cache.HUB, key, COUNT_TTL, load))


async def mark_read(engine, user: Principal, body: ReadRequest) -> ReadResult:
    """Mark the notifications of ``user`` that ``body`` names read, or all of them; audited."""
    ids = None if body.all else list(dict.fromkeys(body.ids or []))
    async with engine.begin() as conn:
        marked = await listing.mark_read(conn, user.user_id, ids)
        if marked:
            target = "notifications:all" if body.all else "notifications:" + ",".join(map(str, marked))
            if body.all:
                target += f" count={len(marked)}"
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.NOTIFICATION_READ, target=target
            )
        unread = (await conn.execute(listing.counts(user.user_id))).one().unread
    log.info("notifications read", extra={"login": user.login, "read": len(marked)})
    return ReadResult(read=len(marked), unread=unread)
