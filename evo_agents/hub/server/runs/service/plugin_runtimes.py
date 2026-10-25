"""The runtimes a run may ask for (``docs/plugins.md``, section Runtimes): the three of the core (``runs.RUNTIMES``) and
the runtimes the plugins a project enabled declare with ``acp`` (``evo_agents.plugins.runtimes``), registered in that
project as ``<plugin>/<name>`` while the plugin is installed, turned on and enabled there
(``plugin_calls.enabled_query``). Schema 0039 keeps only the shape of a name in runs; the registry is the rule.

- ``registered``: a project's registry, each runtime's name with its declaration (None for the core's).
- ``check``: 422 for a dispatch whose runtime the project does not register; ``any`` always passes, and only ever
  takes a runtime of the core.
- ``declared``: the declaration of a plugin's runtime in a project now, which a claim hands the worker; a run whose
  plugin is no longer enabled there by then fails at the claim (``claims``).
- ``offered``: the declarations a worker gets in the answer to its heartbeat: the runtimes of the plugins its owner
  accepted (``workers.plugins``) that a project it serves enabled. Its daemon finds their programs and reports them in
  its runtimes.
- ``claimable``: the runtimes of plugins a worker may claim runs of: reported available by a daemon that says it runs
  them (``runs.PLUGIN_RUNTIMES``), of a plugin its owner accepted. A worker without one claims no run of it, and the
  run waits, queued, for one that has it: it does not fail.
- ``unfit``: why a worker could never claim a run of a plugin's runtime, for a dispatch pinned to it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.plugin_calls import enabled_plugins, enabled_query, manifest_of
from evo_agents.hub.server.runs.models import available
from evo_agents.plugins.manifest import ManifestError
from evo_agents.plugins.runtimes import AcpRuntime, acp_runtimes, is_plugin_runtime, plugin_of

log = logging.getLogger(__name__)

NOTHING = "nothing was dispatched"


def _declared_by(rows) -> dict[str, AcpRuntime]:
    """The ACP runtimes the plugins of ``rows`` (of ``enabled_query``) declare, by name, each once."""
    found: dict[str, AcpRuntime] = {}
    for row in rows:
        try:
            manifest = manifest_of(row)
        except ManifestError:  # stored once it passed; a manifest this release cannot read declares nothing here
            log.warning("a stored manifest does not read", extra={"plugin": row.name, "version": row.installed_version})
            continue
        for runtime in acp_runtimes(manifest):
            found.setdefault(runtime.runtime, runtime)
    return found


async def registered(conn: AsyncConnection, project_id: int) -> dict[str, AcpRuntime | None]:
    """The runtimes project ``project_id`` registers: the core's (None), then its plugins' in plugin order."""
    found: dict[str, AcpRuntime | None] = dict.fromkeys(runs.RUNTIMES)
    found.update(_declared_by(await enabled_plugins(conn, project_id)))
    return found


async def check(conn: AsyncConnection, project_id: int, project: str, runtime: str) -> None:
    """422 when ``runtime`` is neither any nor a runtime project ``project`` registers."""
    if runtime == "any" or runtime in runs.RUNTIMES:
        return
    names = await registered(conn, project_id)
    if runtime in names:
        return
    plugin = plugin_of(runtime)
    why = (
        f"plugin {plugin} is not enabled in project {project}, or declares no runtime {runtime.partition('/')[2]}"
        if plugin
        else f"{runtime} is not a runtime of the core, and a plugin's runtime is named <plugin>/<name>"
    )
    raise HTTPException(
        422, f"runtime {runtime} is not registered in project {project}: {why}; it takes {', '.join(names)}; {NOTHING}"
    )


async def declared(conn: AsyncConnection, project_id: int, runtime: str) -> AcpRuntime | None:
    """The declaration of the plugin's runtime ``runtime`` in project ``project_id`` now; None when it is not
    registered there."""
    plugin = plugin_of(runtime)
    if plugin is None:
        return None
    query = enabled_query().where(tables.plugins.c.name == plugin, tables.projects.c.id == project_id)
    return _declared_by(await conn.execute(query)).get(runtime)


async def offered(conn: AsyncConnection, worker_id: int, plugins: Iterable[str]) -> list[AcpRuntime]:
    """The runtimes of ``plugins`` (the worker's, as its owner set them) that a project worker ``worker_id`` serves
    enabled, by name."""
    plugins = list(plugins or ())
    if not plugins:
        return []
    wp, projects = tables.worker_projects, tables.projects
    served = select(wp.c.project_id).where(wp.c.worker_id == worker_id)
    query = (
        enabled_query()
        .where(tables.plugins.c.name.in_(plugins), projects.c.id.in_(served))
        .order_by(tables.plugins.c.name, projects.c.id)
    )
    found = _declared_by(await conn.execute(query))
    return [found[name] for name in sorted(found)]


def claimable(reported: Mapping | None, plugins: Iterable[str] | None, features: Iterable[str] | None) -> list[str]:
    """The runtimes of plugins a worker claims runs of: those its heartbeat reported available, of a plugin of
    ``plugins``, when its daemon says it runs them."""
    if runs.PLUGIN_RUNTIMES not in (features or ()):
        return []
    accepted = set(plugins or ())
    return sorted(
        name
        for name, report in (reported or {}).items()
        if is_plugin_runtime(name) and plugin_of(name) in accepted and available(report)
    )


def unfit(
    runtime: str,
    *,
    worker_id: int,
    agent_version: str | None,
    features: Iterable[str],
    plugins: Iterable[str],
    runtimes: Mapping,
) -> list[str]:
    """What keeps a worker (as its last heartbeat reported it, and its owner set it) from ever claiming a run of the
    plugin's runtime ``runtime``; empty when nothing does."""
    plugin = plugin_of(runtime)
    if runs.PLUGIN_RUNTIMES not in features:
        return [
            f"its daemon (evo-agents {agent_version or 'of an unknown version'}) does not say it runs the runtimes of "
            "plugins: upgrade it and restart its daemon"
        ]
    if plugin not in set(plugins):
        return [
            f"its owner has not let it take the runtimes of plugin {plugin}: add {plugin} to the worker's plugins, on "
            f"its page or with PATCH /v1/workers/{worker_id}"
        ]
    report = runtimes.get(runtime)
    if not available(report):
        reason = report.get("reason") if isinstance(report, Mapping) else None
        return [f"it does not report {runtime} available{f' ({reason})' if reason else ''}"]
    return []
