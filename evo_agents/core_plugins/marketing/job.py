"""The job ``advance`` of the plugin marketing: for each project that enables it, take the posts the gate has not
decided one step further, read the numbers of the posts that reached +24h or +7d, push the numbers to Signal and read
the replies under the project accounts' recent posts (``replies``; ``docs/marketing-plugin.md``). From Monday 08:00
UTC it tells the project's admins the Growth section of the weekly bulletin, once a week (``weekly``).

A browser post no script reported on in 30 minutes is failed, one nobody claimed in a day a draft (``browser``). A post
waits as ``judging`` until the Judge's run ended; the job reads the answer and goes on from there, so a post a
route proposed and the Judge answered minutes later still goes out without a person. Every post is decided in a
transaction of its own: one that fails leaves the rest their pass. A project that does not enable the plugin gets
nothing written. The cron is every 5 minutes.
"""

from __future__ import annotations

import logging
from datetime import UTC

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.core_plugins.marketing import browser, flow, metrics, push, replies, settings, store, weekly
from evo_agents.hub.server import core_plugin_api as hub

log = logging.getLogger(__name__)


async def run_project(
    engine: AsyncEngine,
    project: str,
    *,
    hub_config=None,
    runtime: flow.Runtime | None = None,
    days: int = push.DEFAULT_DAYS,
) -> dict:
    """One pass over ``project``; what it did, counted. Nothing at all when the project does not enable the plugin."""
    runtime = runtime or flow.Runtime()
    async with engine.begin() as conn:
        try:
            project_id = (await hub.read_project(conn, store.handle(), project)).id
        except hub.Refused:  # no such project, or it does not enable the plugin
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
        done["expired"] = await browser.expire(conn, project, project_id, runtime.now())
    async with engine.begin() as conn:
        done["metrics"] = await metrics.collect(
            conn, cfg, project, project_id, runtime.transport, runtime.now().astimezone(UTC)
        )
        done["signals"] = await push.push_project(conn, project, project_id, days)
    async with engine.begin() as conn:
        done["replies"] = await replies.collect(conn, cfg, project_id, runtime.transport, runtime.now().astimezone(UTC))
    async with engine.begin() as conn:
        done["weekly"] = await weekly.send(conn, cfg, project, project_id, runtime.now())
    return done


async def run(params: dict, context: hub.Context) -> dict:
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
