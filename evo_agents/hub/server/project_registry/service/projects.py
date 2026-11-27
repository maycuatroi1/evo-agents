"""Registering a harness's project and the projects a caller sees (``evo_agents.hub.server.projects``): the checks of
a registration, who may register or update a project, and the projects as a caller sees them, each request in one
transaction."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.harness import channel_sink_id, channel_sink_problem
from evo_agents.hub import kg_semantic
from evo_agents.hub.access import CHANNEL_KIND, HUB_KIND, ProjectRules
from evo_agents.hub.server import audit, invites
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.paging import WHOLE, Page
from evo_agents.hub.server.profile_store import ensure_default, handle_owner
from evo_agents.hub.server.project_registry.models.projects import (
    Clearance,
    Harness,
    Project,
    Registered,
    Registration,
    Repo,
    Sink,
    clearance_fields,
    not_found,
)
from evo_agents.hub.server.project_registry.repository import projects as repository
from evo_agents.hub.server.project_registry.service.access import scoped_grant
from evo_agents.hub.server.security import Principal


def duplicates(names: list[str]) -> list[str]:
    return sorted({name for name in names if names.count(name) > 1})


def checked(name: str, body: Registration) -> ProjectRules:
    """The project's rules from ``body``; 422 naming every reference the ladder does not resolve."""
    problems = []
    for key, names in (("levels", body.levels), ("locations", body.locations)):
        if duplicates(names):
            problems.append(f"{key} lists {', '.join(duplicates(names))} more than once")
    label = body.default_label
    if label.level not in body.levels:
        problems.append(f"the harness source's label level {label.level!r} is not in levels")
    if label.location is not None and label.location not in body.locations:
        problems.append(f"the harness source's label location {label.location!r} is not in locations")
    if duplicates([sink.id for sink in body.sinks]):
        problems.append(f"sinks {', '.join(duplicates([sink.id for sink in body.sinks]))} are declared twice")
    for sink in body.sinks:
        if sink.clearance.level not in body.levels:
            problems.append(f"sink {sink.id}: clearance level {sink.clearance.level!r} is not in levels")
        if sink.clearance.location is not None and sink.clearance.location not in body.locations:
            problems.append(f"sink {sink.id}: clearance location {sink.clearance.location!r} is not in locations")
        if sink.kind == CHANNEL_KIND and not channel_sink_id(sink.id):
            problems.append(f"sink {channel_sink_problem(sink.id)}")
    hubs = [sink.id for sink in body.sinks if sink.kind == HUB_KIND]
    if len(hubs) > 1:
        problems.append(f"sinks {', '.join(hubs)} are all of kind hub; a project declares one")
    if duplicates([repo.name for repo in body.repos]):
        problems.append(f"repos {', '.join(duplicates([repo.name for repo in body.repos]))} are declared twice")
    if problems:
        raise HTTPException(422, f"project {name} cannot be registered: " + "; ".join(problems))
    sinks = [sink.model_dump(exclude_none=True) for sink in body.sinks]
    rules = ProjectRules(name, body.levels, body.locations, sinks)
    rules.default_label = rules.describe(rules.given(label.model_dump(exclude_none=True)))
    return rules


def desired_of(body: Registration, rules: ProjectRules) -> dict:
    """What the hub should hold for the project, in the form ``repository.held`` reads it back."""
    return {
        "levels": body.levels,
        "locations": body.locations,
        "default_label": rules.default_label,
        "harness": (body.harness.name, body.harness.workspace, body.harness.path),
        "sinks": sorted((s.id, s.kind, clearance_fields(s.clearance.model_dump())) for s in body.sinks),
        "repos": sorted((r.name, r.origin, r.default_branch, r.path) for r in body.repos),
    }


async def check_grants(conn: AsyncConnection, name: str, project_id: int, levels: list[str]) -> None:
    """409 when a grant reaches a level the new ladder drops."""
    stranded = await repository.stranded_grants(conn, project_id, levels)
    if stranded:
        held = ", ".join(f"{login} ({level})" for login, level in stranded)
        raise HTTPException(
            409,
            f"the new levels of project {name} drop levels that grants reach: {held}. Change those grants with "
            "`evo-agents hub admin grant` first",
        )


def refusal(name: str) -> str:
    return f"registering or updating project {name} needs a hub admin, or the admin role on project {name}"


def _project(row, user: Principal, sinks: dict[int, list[Sink]], repos: dict[int, list[Repo]]) -> Project:
    harness = Harness(name=row.cluster, workspace=row.workspace, path=row.harness_path) if row.cluster else None
    role, max_level = scoped_grant(user, row.levels, row.role, row.max_level)
    return Project(
        name=row.name,
        harness=harness,
        levels=row.levels,
        locations=row.locations,
        default_label=row.default_label,
        sinks=sinks.get(row.id, []),
        repos=repos.get(row.id, []),
        role=role,
        max_level=max_level,
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
        archived_by=row.archived_by,
    )


async def projects_of(
    conn: AsyncConnection, user: Principal, name: str | None = None, page: Page = WHOLE, *, archived: bool = True
) -> list[Project]:
    """The projects ``user`` sees (only ``name`` when given) on ``page``, with their sinks and repos."""
    rows = await repository.visible_rows(
        conn, user.user_id, user.admin, user.run_project, name, page=page, archived=archived
    )
    ids = [row.id for row in rows]
    sinks: dict[int, list[Sink]] = {}
    repos: dict[int, list[Repo]] = {}
    for sink in await repository.sinks_of_projects(conn, ids):
        clearance = Clearance(**sink.clearance)
        sinks.setdefault(sink.project_id, []).append(Sink(id=sink.sink_id, kind=sink.kind, clearance=clearance))
    for repo in await repository.repos_of_projects(conn, ids):
        repos.setdefault(repo.project_id, []).append(
            Repo(name=repo.name, origin=repo.origin, default_branch=repo.default_branch, path=repo.path)
        )
    return [_project(row, user, sinks, repos) for row in rows]


async def list_projects(engine: AsyncEngine, user: Principal, page: Page, archived: bool) -> list[Project]:
    async with UnitOfWork(engine) as conn:
        return await projects_of(conn, user, page=page, archived=archived)


async def project_view(conn: AsyncConnection, user: Principal, project: str) -> Project:
    """Project ``project`` as ``user`` sees it, archived or not; 404 when they do not see it."""
    found = await projects_of(conn, user, project)
    if not found:
        raise HTTPException(404, not_found(project))
    return found[0]


async def show(engine: AsyncEngine, user: Principal, project: str) -> Project:
    async with UnitOfWork(engine) as conn:
        return await project_view(conn, user, project)


async def refuse_agent_handle(conn: AsyncConnection, name: str) -> None:
    """409 when ``name`` is the handle of an agent of another project: a project's default agent is @<its name>."""
    owner = await handle_owner(conn, name)
    if owner is not None:
        raise HTTPException(
            409,
            f"{name} is the handle of an agent of project {owner}, and @{name} would name both: register the project "
            "under another name; nothing was registered",
        )


async def create_project(conn: AsyncConnection, user: Principal, project: str, fields: dict, top: str) -> int | None:
    """Insert the new project ``project`` as a hub admin, or as a member spending a right to register one, who becomes
    its admin at level ``top``; None when another request registered it a moment ago, and the right stays unused.
    ``register`` and a project made from an upload (``evo_agents.hub.server.kg_upload_project``) call it."""
    right = None if user.admin else await invites.register_right(conn, user)
    if not user.admin and right is None:
        raise HTTPException(403, refusal(project))
    await refuse_agent_handle(conn, project)
    project_id = await repository.insert_project(conn, project, user.user_id, fields)
    if project_id is not None:
        await kg_semantic.start(conn, project_id)
    if project_id is not None and right is not None:
        await invites.spend_register_right(conn, right, user, project_id, top)
    return project_id


async def register(engine: AsyncEngine, user: Principal, project: str, body: Registration) -> Registered:
    """Register ``project`` from its harness, or bring the hub's copy up to date with it."""
    rules = checked(project, body)
    desired = desired_of(body, rules)
    cluster, workspace, harness_path = desired["harness"]
    fields = {
        "levels": desired["levels"],
        "locations": desired["locations"],
        "default_label": desired["default_label"],
        "cluster": cluster,
        "workspace": workspace,
        "harness_path": harness_path,
    }
    async with UnitOfWork(engine) as conn:
        project_id = await repository.locked_id(conn, project)
        created = False
        if project_id is None:
            project_id = await create_project(conn, user, project, fields, desired["levels"][-1])
            created = project_id is not None
            if not created:  # registered by another request a moment ago: this one updates it
                project_id = await repository.locked_id(conn, project, one=True)
        assert project_id is not None
        changed = created
        if not created:
            role = await repository.grant_role(conn, user.user_id, project_id)
            role = scoped_grant(user, body.levels, role, None)[0] if role else None  # a run's agent: writer
            if not user.admin and role != "admin":
                raise HTTPException(403, refusal(project))
            changed = await repository.held(conn, project_id) != desired
            if changed:
                await check_grants(conn, project, project_id, body.levels)
                await repository.update_project(conn, project_id, fields)
        if created:
            await ensure_default(conn, project_id)
        if changed:
            await repository.write_children(conn, project_id, desired)
            action = audit.PROJECT_REGISTER if created else audit.PROJECT_UPDATE
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=project)
        (info,) = await projects_of(conn, user, project)  # a hub admin or a project admin sees it
    return Registered(**info.model_dump(), created=created, changed=changed)
