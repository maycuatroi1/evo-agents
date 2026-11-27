"""The probes of a project as their readers read them, and what other modules call: ``store`` (a proposal's probe, a
review run's, an admin's or a core plugin's rule's), ``attach`` (the probes of a proposal take the change accepting it
made), ``of_proposal`` and ``verify_shown`` (who reads the verify, the answer key)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.memories.service.audience import Reader, label_reaches
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.probes.models.probe import Probe, ProbeIn, ProbeList, view
from evo_agents.hub.server.probes.repository import probes as repository
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal, holds


def verify_shown(access: ProjectAccess, user: Principal) -> bool:
    """Whether ``user`` reads the verify of the project's probes: an admin of it, and not the agent of a run."""
    return holds(access.role, "admin") and user.acts_for_run is None


async def store(
    conn: AsyncConnection,
    *,
    project_id: int,
    probe: ProbeIn,
    source: str,
    label: dict,
    proposal_id: int | None = None,
    change_id: int | None = None,
    user_id: int | None = None,
    run_id: int | None = None,
    replaces: int | None = None,
    plugin_id: int | None = None,
    ref: str | None = None,
    arms: dict | None = None,
) -> int:
    """Store ``probe`` (checked and masked already) for a proposal or a change of the project, or for the rule ``ref``
    of plugin ``plugin_id`` with its ``arms`` (source plugin), in place of probe ``replaces`` if any; its id."""
    values = {
        "project_id": project_id,
        "proposal_id": proposal_id,
        "change_id": change_id,
        "repo": probe.repo,
        "base_sha": probe.commit,
        "task": probe.task,
        "verify": probe.verify,
        "source": source,
        "label": label,
        "created_by": user_id,
        "created_run_id": run_id,
        "replaces": replaces,
        "plugin_id": plugin_id,
        "ref": ref,
        "arms": arms,
    }
    return await repository.insert_probe(conn, values)


async def attach(conn: AsyncConnection, proposal_id: int, change_id: int) -> None:
    """Give the probes of proposal ``proposal_id`` that have no change yet the change accepting it made."""
    await repository.attach(conn, proposal_id, change_id)


async def of_proposal(conn: AsyncConnection, project: str, proposal_id: int, *, shown: bool) -> Probe | None:
    """The newest active probe of proposal ``proposal_id``, as its reader sees it; None without one."""
    found = await repository.newest_active_of_proposal(conn, proposal_id)
    return None if found is None else view(project, found, shown=shown)


async def readable(conn: AsyncConnection, access: ProjectAccess, user: Principal, row, sink: str | None) -> bool:
    """Whether the label of probe ``row`` reaches ``user`` through ``sink`` (else the project's hub sink)."""
    through = plan_service.read_sink(access, sink)
    return await label_reaches(Reader(conn, user), access, row.label, through)  # pyright: ignore[reportArgumentType]


async def held_row(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, sink: str | None, *, lock=False
):
    """Probe ``probe_id`` of the project when its label reaches ``user``; 404 otherwise."""
    row = await repository.of_project(conn, access.project_id, probe_id, lock=lock)
    if row is None or not await readable(conn, access, user, row, sink):
        raise NotFound(f"project {access.name} has no probe {probe_id}")
    return row


async def shown(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int) -> Probe:
    """Probe ``probe_id`` as ``user`` reads it."""
    row = await repository.by_id(conn, probe_id)
    return view(access.name, row, shown=verify_shown(access, user))


async def listed(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    *,
    change: int | None,
    proposal: int | None,
    state: str | None,
    limit: int,
    sink: str | None,
) -> ProbeList:
    """The project's probes ``user`` may read, newest first, at most ``limit``, and how many there are."""
    rows = await repository.listed(conn, access.project_id, change=change, proposal=proposal, state=state)
    found = [row for row in rows if await readable(conn, access, user, row, sink)]
    seen = verify_shown(access, user)
    return ProbeList(probes=[view(access.name, row, shown=seen) for row in found[:limit]], total=len(found))


async def one(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, sink: str | None) -> Probe:
    """Probe ``probe_id`` of the project as ``user`` reads it; 404 when its label does not reach them."""
    row = await held_row(conn, access, user, probe_id, sink)
    return view(access.name, row, shown=verify_shown(access, user))
