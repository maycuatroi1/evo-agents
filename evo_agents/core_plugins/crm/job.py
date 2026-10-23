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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.core_plugins.crm import github, intake, push, settings, staging
from evo_agents.hub import signals, tables
from evo_agents.hub.plugin_calls import CoreContext

log = logging.getLogger(__name__)


async def run_project(
    engine: AsyncEngine, project: str, *, fetcher: github.Fetcher | None = None, days: int = push.DEFAULT_DAYS
) -> dict:
    """One pass over ``project``; what it did, counted. Nothing at all when the project does not enable the plugin."""
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(tables.projects.c.id).where(tables.projects.c.name == project))
        row = await settings.enabled(conn, project_id) if project_id is not None else None
        if row is None:
            return {}
        config = await settings.config(conn, row, project_id)
        label = (await signals.project_rules(conn, project_id)).default_label
        done: dict = {"hub": await intake.from_hub(conn, project_id, label)}
        if config.repo:
            done["github"] = await intake.from_github(
                conn, project_id, label, config.repo, fetcher or github.default_fetcher()
            )
        done["stages"] = await staging.refresh(conn, project_id)
        done["signals"] = await push.push_counts(conn, project_id, days)
    log.info("crm intake", extra={"project": project, **{k: v for k, v in done.items() if k != "signals"}})
    return done


async def run(params: dict, context: CoreContext) -> dict:
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
