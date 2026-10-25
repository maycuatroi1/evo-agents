"""The job ``intake`` of the plugin crm: for each project that enables it, find the contacts, compute their stages and
push the stage counts to Signal (``docs/crm-plugin.md``).

One pass of a project: read the invites and grants of the hub and, when the config names a ``repo``, its issues, pull
requests and discussions (``intake``); compute the stage of every contact (``staging``); push the counts of each
stage (``push``). A project that does not enable the plugin gets nothing written, and one whose GitHub cannot be read
still gets the rest. The cron is every 15 minutes: every step is idempotent, so a pass that finds nothing new adds no
row.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.core_plugins.crm import github, intake, push, settings, staging, store
from evo_agents.hub.server import core_plugin_api as hub

log = logging.getLogger(__name__)


async def run_project(
    engine: AsyncEngine,
    project: str,
    *,
    fetcher: github.Fetcher | None = None,
    days: int = push.DEFAULT_DAYS,
) -> dict:
    """One pass over ``project``; what it did, counted. Nothing at all when the project does not enable the plugin."""
    async with engine.begin() as conn:
        try:
            found = await hub.read_project(conn, store.handle(), project)
        except hub.Refused:  # no such project, or it does not enable the plugin
            return {}
        config = await settings.config(conn, project)
        done: dict = {"hub": await intake.from_hub(conn, found)}
        if config.repo:
            done["github"] = await intake.from_github(conn, found, config.repo, fetcher or github.default_fetcher())
        done["stages"] = await staging.refresh(conn, found)
        done["signals"] = await push.push_counts(conn, project, found.id, days)
    log.info("crm intake", extra={"project": project, **{k: v for k, v in done.items() if k != "signals"}})
    return done


async def run(params: dict, context: hub.Context) -> dict:
    """The job ``intake``: one pass over each project ``params`` names (``projects``)."""
    days = getattr(context.config, "signal_days", None)
    found = {}
    for project in params.get("projects") or []:
        try:
            found[project] = await run_project(
                context.engine, project, days=days if isinstance(days, int) else push.DEFAULT_DAYS
            )
        except Exception:  # one project's failure leaves the others their pass
            log.exception("crm intake failed", extra={"project": project})
            found[project] = {"failed": True}
    return {"projects": found}


__all__ = ["run", "run_project"]
