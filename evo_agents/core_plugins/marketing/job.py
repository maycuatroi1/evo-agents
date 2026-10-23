"""The job ``advance`` of the plugin marketing: for each project that enables it, take the posts the gate has not
decided one step further, read the numbers of the posts that reached +24h or +7d, and push the numbers to Signal
(``docs/marketing-plugin.md``).

A post waits as ``judging`` until the Judge's run ended; the job reads the answer and goes on from there, so a post a
route proposed and the Judge answered minutes later still goes out without a person. Every post is decided in a
transaction of its own: one that fails leaves the rest their pass. A project that does not enable the plugin gets
nothing written. The cron is every 5 minutes.
"""

from __future__ import annotations

import logging
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.core_plugins.marketing import flow, metrics, push, settings, store
from evo_agents.hub import tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.plugin_calls import CoreContext

log = logging.getLogger(__name__)


async def run_project(
    engine: AsyncEngine,
    project: str,
    *,
    hub_config: HubConfig | None = None,
    runtime: flow.Runtime | None = None,
    days: int = push.DEFAULT_DAYS,
) -> dict:
    """One pass over ``project``; what it did, counted. Nothing at all when the project does not enable the plugin."""
    runtime = runtime or flow.Runtime()
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(tables.projects.c.id).where(tables.projects.c.name == project))
        if project_id is None or await settings.enabled(conn, project_id) is None:
            return {}
        cfg = await settings.config(conn, hub_config, project)
        waiting = [post.id for post in await store.open_posts(conn, project_id)]
    done: dict = {"posts": {}}
    for post_id in waiting:
        try:
            async with engine.begin() as conn:
                state = await flow.decide(conn, runtime, cfg, project, project_id, post_id)
            done["posts"][post_id] = state
        except Exception:  # one post's failure leaves the others their pass
            log.exception("marketing post failed", extra={"project": project, "post": post_id})
            done["posts"][post_id] = "error"
    async with engine.begin() as conn:
        done["metrics"] = await metrics.collect(
            conn, cfg, project, project_id, runtime.transport, runtime.now().astimezone(UTC)
        )
        done["signals"] = await push.push_project(conn, project_id, days)
    return done


async def run(params: dict, context: CoreContext) -> dict:
    """The job ``advance``: one pass over each project ``params`` names (``projects``)."""
    days = getattr(context.config, "signal_days", None)
    found = {}
    for project in params.get("projects") or []:
        try:
            found[project] = await run_project(
                context.engine,
                project,
                hub_config=context.config,
                days=days if isinstance(days, int) else push.DEFAULT_DAYS,
            )
        except Exception:  # one project's failure leaves the others their pass
            log.exception("marketing advance failed", extra={"project": project})
            found[project] = {"failed": True}
    return {"projects": found}
