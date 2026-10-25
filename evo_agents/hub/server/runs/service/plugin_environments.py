"""The environments a run's agent may run in (``docs/plugins.md``, section Environments): ``host``, the worker's own
machine, and the environments the plugins a project enabled declare (``evo_agents.plugins.run_environments``),
registered in that project as ``<plugin>/<name>`` while the plugin is installed, turned on and enabled there
(``plugin_calls.enabled_query``). The environment axis of the run's agent profile names them, in order of preference
(``evo_agents.hub.profiles``); a profile whose axis allows all runs on the host.

- ``registered``: a project's environments, each name with its declaration (None for host).
- ``check``: 422 for a dispatch under a profile that allows no environment the project registers.
- ``offered``: the environments of plugins a worker takes: those the projects it serves enabled, of the plugins its
  owner accepted (``workers.plugins``), when its daemon says it runs environments (``runs.ENVIRONMENTS``). A worker
  without one claims no run whose profile allows only environments of plugins, and the run waits, queued, for one
  that has it: it does not fail.
- ``picked``: the environment a claim picks, the first of the profile's allowlist that is host or offered and
  registered in the run's project, with the declaration the claim hands the worker; a run whose allowlist names none
  by then (the plugin disabled there since) fails at the claim (``claims``).
- ``unfit``: what keeps a pinned worker from ever claiming a run of the profile, for the 409 of a dispatch.

Whether the worker can run a container at all (an engine, the image) is its preflight's to say: a run it cannot run
there fails before its agent starts, with cause environment.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, runs, tables
from evo_agents.hub.plugin_calls import enabled_plugins, enabled_query, manifest_of
from evo_agents.plugins.manifest import ManifestError
from evo_agents.plugins.run_environments import HOST, ContainerEnvironment, container_environments, plugin_of

log = logging.getLogger(__name__)

NOTHING = "nothing was dispatched"


def _declared_by(rows) -> dict[str, ContainerEnvironment]:
    """The environments the plugins of ``rows`` (of ``enabled_query``) declare, by name, each once."""
    found: dict[str, ContainerEnvironment] = {}
    for row in rows:
        try:
            manifest = manifest_of(row)
        except ManifestError:  # stored once it passed; a manifest this release cannot read declares nothing here
            log.warning("a stored manifest does not read", extra={"plugin": row.name, "version": row.installed_version})
            continue
        for environment in container_environments(manifest):
            found.setdefault(environment.environment, environment)
    return found


async def registered(conn: AsyncConnection, project_id: int) -> dict[str, ContainerEnvironment | None]:
    """The environments project ``project_id`` registers: host (None), then its plugins' in plugin order."""
    found: dict[str, ContainerEnvironment | None] = {HOST: None}
    found.update(_declared_by(await enabled_plugins(conn, project_id)))
    return found


def allowlist(permissions: dict | None) -> list[str] | None:
    """The environment axis of ``permissions`` as an allowlist, in its order; None when it allows all (host)."""
    return profiles.allowed(permissions, "environment")


async def check(conn: AsyncConnection, project_id: int, project: str, handle: str, permissions: dict) -> list[str]:
    """The environments of profile ``handle``'s allowlist that project ``project`` registers, in order; empty for a
    profile whose axis allows all. 422 when the allowlist names none it registers."""
    allow = allowlist(permissions)
    if allow is None:
        return []
    names = await registered(conn, project_id)
    usable = [name for name in allow if name in names]
    if usable:
        return usable
    if not allow:
        raise HTTPException(422, f"agent @{handle} allows no environment, so its runs run nowhere; {NOTHING}")
    raise HTTPException(
        422,
        f"agent @{handle} allows only the environments {', '.join(allow)}, none of which project {project} "
        f"registers: enable the plugin that declares one, or allow {HOST}; it registers {', '.join(names)}; {NOTHING}",
    )


async def offered(
    conn: AsyncConnection, worker_id: int, plugins: Iterable[str] | None, features: Iterable[str] | None
) -> list[str]:
    """The names of the plugins' environments worker ``worker_id`` takes: of ``plugins`` (its owner's choice), enabled
    in a project it serves, when its daemon says ``runs.ENVIRONMENTS``; sorted."""
    plugins = list(plugins or ())
    if runs.ENVIRONMENTS not in (features or ()) or not plugins:
        return []
    wp, projects = tables.worker_projects, tables.projects
    served = select(wp.c.project_id).where(wp.c.worker_id == worker_id)
    query = (
        enabled_query()
        .where(tables.plugins.c.name.in_(plugins), projects.c.id.in_(served))
        .order_by(tables.plugins.c.name, projects.c.id)
    )
    return sorted(_declared_by(await conn.execute(query)))


async def picked(
    conn: AsyncConnection, project_id: int, rule: dict | None, offered_names: Iterable[str]
) -> tuple[str, ContainerEnvironment | None] | None:
    """The environment a claim picks in project ``project_id`` for a run whose profile's environment axis is ``rule``,
    with its declaration as the project has it now: the first of the allowlist that is host, or one of
    ``offered_names`` the project registers; host for an axis that allows all, or a run without a profile. None when
    the allowlist names no such one: what the worker takes is no longer registered in the project."""
    allow = allowlist({"environment": rule} if rule else None)
    if allow is None:
        return HOST, None
    takes = set(offered_names)
    names = await registered(conn, project_id) if takes & set(allow) else {}
    for name in allow:
        if name == HOST:
            return HOST, None
        if name in takes and names.get(name) is not None:
            return name, names[name]
    return None


def unfit(usable: list[str], *, worker_id: int, agent_version: str | None, features, plugins) -> str | None:
    """What keeps a worker (as its last heartbeat reported it, and its owner set it) from ever claiming a run whose
    profile allows the environments ``usable`` (those its project registers); None when nothing does."""
    if not usable or HOST in usable:
        return None
    if runs.ENVIRONMENTS not in set(features or ()):
        return (
            f"its daemon (evo-agents {agent_version or 'of an unknown version'}) does not say it runs environments, "
            f"and the run's agent runs only in {', '.join(usable)}: upgrade it and restart its daemon"
        )
    accepted = set(plugins or ())
    if not any(plugin_of(environment) in accepted for environment in usable):
        wanted = sorted({plugin_of(environment) for environment in usable})
        return (
            f"its owner has not let it take the environments of plugin {', '.join(wanted)}: add one to the worker's "
            f"plugins, on its page or with PATCH /v1/workers/{worker_id}"
        )
    return None
