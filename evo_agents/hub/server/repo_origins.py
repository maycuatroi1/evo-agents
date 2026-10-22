"""Where a run's repos come from: the origin and the path of each, for the dispatch that checks a run can reach them,
the leases of a run (``credentials``) and the worker that clones a repo it has no checkout of.

A repo's origin is the one the run's project registers for it (project_repos). A repo the project does not list, or
lists without an origin, takes the origin another project registers for a repo of that name, among the projects the
run's owner holds a grant on, when they all agree on it (compared as ``credentials.normalize_origin`` writes it): a plan
of one project may work in the harness or a repo of another (a plan of hosteye that registers its hostnames in the
harness of ialab). Two projects that name different origins for it give none, as does a repo no project registers.
The path is the one the project that gave the origin registers, relative to its workspace, so the worker places a
clone of it where ``evo-agents hub registry pull`` would."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import normalize_origin


@dataclass(frozen=True)
class RepoOrigin:
    origin: str
    path: str | None  # as the project registered it: relative to its workspace, ~/..., or absolute
    project: str  # the project that registered it: the run's, or the other one the owner holds a grant on


async def repo_origins(
    conn: AsyncConnection, project_id: int, owner_id: int, names: Iterable[str]
) -> dict[str, RepoOrigin]:
    """The origin of each of ``names`` that has one (see the module's docstring), for a run of project ``project_id``
    dispatched by ``owner_id``; a repo without one is left out."""
    wanted = list(dict.fromkeys(name for name in names if isinstance(name, str) and name))
    if not wanted:
        return {}
    pr, p, g = tables.project_repos, tables.projects, tables.grants
    own = await conn.execute(
        select(pr.c.name, pr.c.origin, pr.c.path, p.c.name.label("project"))
        .join_from(pr, p, p.c.id == pr.c.project_id)
        .where(pr.c.project_id == project_id, pr.c.name.in_(wanted), pr.c.origin.is_not(None))
    )
    found = {row.name: RepoOrigin(row.origin, row.path, row.project) for row in own if row.origin.strip()}
    left = [name for name in wanted if name not in found]
    if not left:
        return found
    others = await conn.execute(
        select(pr.c.name, pr.c.origin, pr.c.path, p.c.name.label("project"))
        .join_from(pr, p, p.c.id == pr.c.project_id)
        .join(g, (g.c.project_id == pr.c.project_id) & (g.c.user_id == owner_id))
        .where(pr.c.project_id != project_id, pr.c.name.in_(left), pr.c.origin.is_not(None))
        .order_by(p.c.name)
    )
    elsewhere: dict[str, list[RepoOrigin]] = {}
    for row in others:
        if row.origin.strip():
            elsewhere.setdefault(row.name, []).append(RepoOrigin(row.origin, row.path, row.project))
    for name, held in elsewhere.items():
        if len({normalize_origin(entry.origin) for entry in held}) == 1:
            found[name] = held[0]
    return found
