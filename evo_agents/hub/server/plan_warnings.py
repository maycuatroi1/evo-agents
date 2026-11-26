"""The warnings a plan is stored with, out of ``plans``: those of plan.schema.json and of ``plan_semantics``, which
checks the repos a plan names against the repos registered for its project (``project_repos``) and its harness repo."""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.harness import load_schema, plan_semantics
from evo_agents.hub import tables
from evo_agents.hub.server.plan_models import Problem
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.schema import validate


class Registered:
    """What ``plan_semantics`` asks of a harness, from what the project registered."""

    def __init__(self, names: set[str]):
        self.names = names

    def known_repos(self) -> set[str]:
        return self.names


async def known_repos(conn: AsyncConnection, access: ProjectAccess) -> Registered:
    """The repos registered for the project of ``access``, and its harness repo by its directory name."""
    repos, projects = tables.project_repos, tables.projects
    names = set((await conn.execute(select(repos.c.name).where(repos.c.project_id == access.project_id))).scalars())
    harness = select(projects.c.harness_path).where(projects.c.id == access.project_id)
    harness_path = (await conn.execute(harness)).scalar()
    if harness_path:
        names.add(PurePosixPath(harness_path).name)  # the harness repo goes by its directory name, as in the loader
    return Registered(names)


async def warnings(conn: AsyncConnection, access: ProjectAccess, body: dict, area: str) -> list[Problem]:
    """The warnings of plan.schema.json and ``plan_semantics`` for ``body`` written to ``area``: a plan is stored
    with them."""
    path = PurePosixPath("plans", area, f"{body['id']}.yaml")
    registered = await known_repos(conn, access)
    semantics = plan_semantics(body, path, registered)  # pyright: ignore[reportArgumentType]  # duck-types Path, Harness
    issues = validate(body, load_schema("plan")) + semantics
    return [Problem(path=i.path, message=i.message) for i in issues if i.severity == "warning"]
