"""A proposed post as the route ``POST /posts`` and the tool ``marketing__propose`` take it: the row it becomes, with
the canvas claim it tests and the Signal key its numbers count toward, and whether the gate decides it now or at its
time (plan autonomous-oss-growth, step 19).

A post scheduled for a time to come is kept as ``proposed`` with ``scheduled_at`` and decided by no one until the Loop
releases it (``loop``): its time came and the project's kill switch is off, so a ``growth_publish`` work item of the
Loop runs and sets ``released_at``, and the job ``advance`` takes it through the gate like any post. A claim is named by
its code (CL-<n>) and must be one of the project's.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import flow, store
from evo_agents.core_plugins.marketing.models import MarketingPostIn
from evo_agents.core_plugins.marketing.settings import Config
from evo_agents.hub.server import core_plugin_api as hub


async def claim_id_of(conn: AsyncConnection, project: str, code: str | None) -> int | None:
    """The id of claim ``code`` (CL-<n>) of the project; None for none; 422 when the project has no such claim."""
    if code is None:
        return None
    found = next(
        (c.id for c in await hub.read_claim_cards(conn, store.handle(), project, numbers=[int(code.split("-")[1])])),
        None,
    )
    if found is None:
        raise store.MarketingError(422, f"no claim {code} on the canvas of this project")
    return found


async def values_of(
    conn: AsyncConnection, project: str, project_id: int, user_id: int | None, body: MarketingPostIn, row
) -> dict:
    """The row of marketing_posts ``body`` proposes on channel ``row``; 422 when it does not read."""
    if body.kind == "reply" and not body.reply_to:
        raise store.MarketingError(422, "a reply names what it answers (reply_to)")
    return {
        "project_id": project_id,
        "channel_id": row.id,
        "channel": row.channel,
        "account": row.account,
        "kind": body.kind,
        "reply_to": body.reply_to,
        "body": body.body,
        "links": body.links,
        "sources": [s.model_dump() for s in body.sources],
        "run_id": body.run_id,
        "proposed_by": user_id,
        "claim_id": await claim_id_of(conn, project, body.claim),
        "signal_key": body.signal_key,
        "scheduled_at": body.scheduled_at,
    }


def waits(values: dict, now: datetime) -> bool:
    """Whether the post is scheduled for a time to come: the gate does not decide it yet."""
    at = values.get("scheduled_at")
    return at is not None and at > now


async def submit(
    conn: AsyncConnection, rt: flow.Runtime, cfg: Config, project: str, project_id: int, values: dict
) -> int:
    """Keep the post and take it through the gate now, or leave it for the Loop at its time; its id. A time already
    past releases the post at once."""
    now = rt.now()
    later = waits(values, now)
    if values.get("scheduled_at") is not None and not later:
        values = {**values, "released_at": now}
    post_id = await store.add_post(conn, **values)
    if not later:
        await flow.decide(conn, rt, cfg, project, project_id, post_id)
    return post_id
