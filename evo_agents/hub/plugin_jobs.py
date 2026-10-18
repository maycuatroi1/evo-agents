"""The periodic jobs of plugins, run through the hub's job queue (``docs/plugins.md``, section Extension points).

A jobs contribution names when it runs with five cron fields in UTC (``cron``, hourly at minute 0 when it names none).
The job hub.plugin_jobs runs every minute (``evo_agents.hub.worker``): for the minute procrastinate scheduled it for, it
finds each job of a plugin that is installed, turned on and enabled in at least one project, whose cron matches that
minute, and defers one job hub.plugin_job for it with the queueing lock ``plugin-job:<plugin>:<job>``, so a job that
is still waiting is not queued twice and a slow one does not pile up. hub.plugin_job checks the plugin again and calls
its contribution (``plugin_calls.call``, method ``job`` of plugin/1) with the projects that enable it, within
JOB_TIMEOUT. A cron that does not read is logged and never fires.

Outside ``evo_agents.hub.server``: the job worker imports it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import jobs, tables
from evo_agents.hub.plugin_calls import CoreContext, PluginHostClient, call, enabled_query

log = logging.getLogger(__name__)

DEFAULT_CRON = "0 * * * *"
JOB_TIMEOUT = 120.0  # seconds one run of a plugin's job may take, the plugin host's own bound


def lock(plugin: str, job: str) -> str:
    """The queueing lock of a plugin's job: at most one of it waits."""
    return f"plugin-job:{plugin}:{job}"


async def _enabled(engine: AsyncEngine, name: str | None = None) -> dict[str, tuple]:
    """plugin name -> (a row of ``enabled_query``, the names of the projects that enable it)."""
    query = enabled_query().order_by(tables.plugins.c.name, tables.projects.c.name)
    if name is not None:
        query = query.where(tables.plugins.c.name == name)
    found: dict[str, tuple] = {}
    async with engine.begin() as conn:
        for row in await conn.execute(query):
            first, projects = found.get(row.name, (row, []))
            found[row.name] = (first, [*projects, row.project])
    return found


def matches(cron: str, at: datetime) -> bool:
    """Whether ``cron`` takes the minute ``at``; False, logged, for a cron that does not read."""
    from croniter import croniter

    try:
        return bool(croniter.match(cron, at.replace(second=0, microsecond=0)))
    except (ValueError, KeyError) as exc:
        log.warning("a plugin's cron does not read", extra={"cron": cron, "error": str(exc)})
        return False


async def due(engine: AsyncEngine, at: datetime) -> list[tuple[str, str]]:
    """(plugin, job) of every job of an enabled plugin whose cron takes the minute ``at``."""
    found = []
    for name, (row, _) in (await _enabled(engine)).items():
        for item in (row.manifest.get("contributes") or {}).get("jobs") or []:
            if matches(item.get("cron", DEFAULT_CRON), at):
                found.append((name, item["name"]))
    return found


async def fire(engine: AsyncEngine, job_manager, timestamp: int | None = None) -> dict:
    """One pass of hub.plugin_jobs: a hub.plugin_job for each job due at ``timestamp`` (now without one)."""
    at = datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else datetime.now(UTC)
    queue, deferred = jobs.JobQueue(job_manager), []
    for plugin, job in await due(engine, at):
        if await queue.defer(jobs.PLUGIN_JOB, queueing_lock=lock(plugin, job), plugin=plugin, job=job) is not None:
            deferred.append(f"{plugin}:{job}")
    if deferred:
        log.info("plugin jobs queued", extra={"jobs": deferred})
    return {"deferred": deferred}


async def run_job(engine: AsyncEngine, config, plugin: str, job: str, *, host=None) -> dict:
    """One run of ``job`` of ``plugin``, while it is enabled somewhere and still contributes it."""
    found = (await _enabled(engine, plugin)).get(plugin)
    if found is None:
        log.info("plugin job skipped: no project enables the plugin", extra={"contribution": f"{plugin}:{job}"})
        return {"skipped": "no project enables the plugin"}
    row, projects = found
    item = next((i for i in (row.manifest.get("contributes") or {}).get("jobs") or [] if i["name"] == job), None)
    if item is None:
        return {"skipped": f"{plugin} {row.installed_version} has no job {job}"}
    own = host is None and config is not None
    if own:
        host = PluginHostClient(config.plugin_host_url, config.plugin_host_token)
    try:
        params = {"job": job, "projects": projects}
        result = await call(
            host, plugin, row.tier, "jobs", item, params, CoreContext(plugin, engine, config), JOB_TIMEOUT
        )
    finally:
        if own:
            await host.aclose()
    log.info("plugin job ran", extra={"contribution": f"{plugin}:{job}", "projects": len(projects)})
    return {"plugin": plugin, "job": job, "projects": projects, "result": result}
