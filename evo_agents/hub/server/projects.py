"""Projects: registering a harness's project, the projects a caller sees, and a caller's access to one of them.

PUT /v1/projects/{project} takes what ``evo-agents hub project register`` read in a harness: the ladder (levels and
locations) and the sinks of its knowledge.yaml, the default label of its memories and plans (the harness
source's), its repos, and where the harness and the repos sit relative to a workspace. Registering a new project
needs a hub admin; registering it again, to bring the hub up to date with its harness, needs a hub admin or the
admin role on the project. The same body twice changes nothing and writes no audit row. A change replaces the sinks
and repos in the same transaction and adds one audit row naming the project, never what it holds; a change that
drops a level some grant reaches is refused until the grant is changed.

GET /v1/projects lists the projects the caller holds a grant on, all of them for a hub admin, with the caller's
role and max level; GET /v1/projects/{project} is one of them. A project that is not registered and one the caller
cannot see get the same 404.

``project_access`` is where the routes for memories, plans, skills and the knowledge graph start: the project's
rules (``evo_agents.hub.access``) and the caller's grant, with the read and write rules applied through them. A hub
admin without a grant manages a project but reads and pushes nothing in it. The agent of a run, on the hub's /mcp
with its worker's token (``Principal.scope``), sees the run's project alone, every other one answering as a project
that is not registered, and there uses its owner's grant capped by the scope (``scoped_grant``): the lower role and
the lower level of the two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import ScalarSelect, and_, bindparam, delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.harness import load_schema
from evo_agents.hub import tables
from evo_agents.hub.access import HUB_KIND, INTEGRITIES, ROLES, ProjectRules, Refused
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

SINK_KINDS = tuple(load_schema("knowledge")["$defs"]["sink"]["properties"]["kind"]["enum"])
PRINTABLE = r"^[^\x00-\x1f\x7f]+$"
Name = Annotated[str, Field(min_length=1, max_length=100, pattern=PRINTABLE)]
PathText = Annotated[str, Field(min_length=1, max_length=4096, pattern=PRINTABLE)]
REFUSALS = {403: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}}

router = APIRouter(prefix="/v1/projects", tags=["projects"], responses={401: {"model": ErrorBody}})


class Clearance(BaseModel):
    level: Name
    location: Name | None = None


class Sink(BaseModel):
    id: Name
    kind: Literal[SINK_KINDS]  # the kinds of knowledge.schema.json
    clearance: Clearance


class LabelIn(BaseModel):
    level: Name
    location: Name | None = Field(None, description="the lowest location of the ladder when left out")
    integrity: Literal[INTEGRITIES] = "U"


class Repo(BaseModel):
    name: Name
    origin: str | None = Field(None, max_length=2048, pattern=PRINTABLE)
    default_branch: str | None = Field(None, max_length=255, pattern=PRINTABLE)
    path: PathText | None = Field(None, description="relative to the workspace when inside it, else ~/... or absolute")


class Harness(BaseModel):
    name: str = Field(pattern=PROJECT_NAME, description="the name in harness.yaml, the cluster's in the registry")
    workspace: PathText = Field(description="~/... when under the home directory of the machine that registered")
    path: PathText = Field(description="the harness root, relative to the workspace when inside it")


class Registration(BaseModel):
    levels: list[Name] = Field(min_length=1, max_length=32, description="lowest first, as in knowledge.yaml")
    locations: list[Name] = Field(min_length=1, max_length=32, description="least restricted first")
    default_label: LabelIn = Field(description="the harness source's label: memories and plans get it by default")
    sinks: list[Sink] = Field(default_factory=list, max_length=64)
    repos: list[Repo] = Field(default_factory=list, max_length=1000)
    harness: Harness


class Project(BaseModel):
    name: str
    harness: Harness | None = Field(description="null for a project registered without its harness paths")
    levels: list[str]
    locations: list[str]
    default_label: dict
    sinks: list[Sink]
    repos: list[Repo]
    role: str | None = Field(description="the caller's role; null for a hub admin without a grant")
    max_level: str | None
    created_at: datetime
    updated_at: datetime


class Registered(Project):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this")


@dataclass(frozen=True)
class ProjectAccess:
    """One caller in one project: the project's rules and the caller's grant (role and max level are None
    without one)."""

    project_id: int
    rules: ProjectRules
    role: str | None
    max_level: str | None

    @property
    def name(self) -> str:
        return self.rules.name

    def visible(self, label, sink: str) -> bool:
        """The read rule for an object carrying ``label`` (as stored), read through ``sink`` of the project."""
        return self.rules.visible(label, self.max_level, sink)

    def push_label(self, label=None) -> dict:
        """The label to store a pushed object with: ``label``, or the project's default without one. Raises the
        403 or 422 of the write rule; call it before writing anything, in the transaction that writes."""
        try:
            return self.rules.check_push(label, self.role)
        except Refused as exc:
            raise HTTPException(exc.status, str(exc)) from None


def not_found(name: str) -> str:
    return f"no project {name} that you can see on this hub: see `evo-agents hub project list`"


def _lower(ladder, held, cap):
    """The lower of ``held`` and ``cap`` on ``ladder``; None when either is not on it."""
    if held not in ladder or cap not in ladder:
        return None
    return held if ladder.index(held) <= ladder.index(cap) else cap


def scoped_grant(user: Principal, levels, role: str | None, max_level: str | None) -> tuple:
    """(role, max level) of the grant ``user`` holds, as ``user`` may use it: capped by a run's scope, when it has
    one, to the lower role and the lower level (on ``levels``, the project's ladder) of the grant and the scope."""
    if user.scope is None or role is None:
        return role, max_level
    return _lower(ROLES, role, user.scope.role), _lower(levels, max_level, user.scope.max_level)


def _with_grant(user_id: int):
    """Projects left joined to the grant ``user_id`` holds on each, if any."""
    projects, grants = tables.projects, tables.grants
    return projects.outerjoin(grants, and_(grants.c.project_id == projects.c.id, grants.c.user_id == user_id))


def _sinks_of(project_id) -> ScalarSelect:
    """The sinks of project ``project_id`` as one JSONB array of {id, kind, clearance}, by id; [] without one."""
    ps = tables.project_sinks
    sink = func.jsonb_build_object("id", ps.c.sink_id, "kind", ps.c.kind, "clearance", ps.c.clearance)
    listed = func.jsonb_agg(aggregate_order_by(sink, ps.c.sink_id), type_=JSONB)
    return (
        select(func.coalesce(listed, literal([], JSONB), type_=JSONB)).where(ps.c.project_id == project_id)
    ).scalar_subquery()


# Built once, as every request that names a project runs it (docs/hub.md, Data access): project :name, the grant
# user :user_id holds on it, and its sinks, in one round trip.
_ACCESS = (
    select(
        tables.projects.c.id,
        tables.projects.c.levels,
        tables.projects.c.locations,
        tables.projects.c.default_label,
        tables.grants.c.role,
        tables.grants.c.max_level,
        _sinks_of(tables.projects.c.id).label("sinks"),
    )
    .select_from(_with_grant(bindparam("user_id")))
    .where(tables.projects.c.name == bindparam("name"))
)


async def project_access(conn: AsyncConnection, user: Principal, name: str) -> ProjectAccess:
    """``user``'s access to project ``name``; 404 when it is not registered, or when ``user`` holds no grant on it
    and is no hub admin, so a name tells nothing about the projects one cannot see. The agent of a run sees the run's
    project alone, with the grant ``scoped_grant`` leaves it."""
    if not user.reaches(name):
        raise HTTPException(404, not_found(name))
    row = (await conn.execute(_ACCESS, {"user_id": user.user_id, "name": name})).first()
    if row is None or (row.role is None and not user.admin):
        raise HTTPException(404, not_found(name))
    role, max_level = scoped_grant(user, row.levels, row.role, row.max_level)
    sinks = row.sinks
    rules = ProjectRules(name, row.levels, row.locations, sinks, row.default_label)
    return ProjectAccess(row.id, rules, role, max_level)


# Registration


def _duplicates(names: list[str]) -> list[str]:
    return sorted({name for name in names if names.count(name) > 1})


def _checked(name: str, body: Registration) -> ProjectRules:
    """The project's rules from ``body``; 422 naming every reference the ladder does not resolve."""
    problems = []
    for key, names in (("levels", body.levels), ("locations", body.locations)):
        if _duplicates(names):
            problems.append(f"{key} lists {', '.join(_duplicates(names))} more than once")
    label = body.default_label
    if label.level not in body.levels:
        problems.append(f"the harness source's label level {label.level!r} is not in levels")
    if label.location is not None and label.location not in body.locations:
        problems.append(f"the harness source's label location {label.location!r} is not in locations")
    if _duplicates([sink.id for sink in body.sinks]):
        problems.append(f"sinks {', '.join(_duplicates([sink.id for sink in body.sinks]))} are declared twice")
    for sink in body.sinks:
        if sink.clearance.level not in body.levels:
            problems.append(f"sink {sink.id}: clearance level {sink.clearance.level!r} is not in levels")
        if sink.clearance.location is not None and sink.clearance.location not in body.locations:
            problems.append(f"sink {sink.id}: clearance location {sink.clearance.location!r} is not in locations")
    hubs = [sink.id for sink in body.sinks if sink.kind == HUB_KIND]
    if len(hubs) > 1:
        problems.append(f"sinks {', '.join(hubs)} are all of kind hub; a project declares one")
    if _duplicates([repo.name for repo in body.repos]):
        problems.append(f"repos {', '.join(_duplicates([repo.name for repo in body.repos]))} are declared twice")
    if problems:
        raise HTTPException(422, f"project {name} cannot be registered: " + "; ".join(problems))
    sinks = [sink.model_dump(exclude_none=True) for sink in body.sinks]
    rules = ProjectRules(name, body.levels, body.locations, sinks)
    rules.default_label = rules.describe(rules.given(label.model_dump(exclude_none=True)))
    return rules


def _clearance(clearance: dict) -> dict:
    return {key: clearance[key] for key in ("level", "location") if clearance.get(key) is not None}


def _desired(body: Registration, rules: ProjectRules) -> dict:
    """What the hub should hold for the project, in the form ``_held`` reads it back."""
    return {
        "levels": body.levels,
        "locations": body.locations,
        "default_label": rules.default_label,
        "harness": (body.harness.name, body.harness.workspace, body.harness.path),
        "sinks": sorted((s.id, s.kind, _clearance(s.clearance.model_dump())) for s in body.sinks),
        "repos": sorted((r.name, r.origin, r.default_branch, r.path) for r in body.repos),
    }


async def _held(conn: AsyncConnection, project_id: int) -> dict:
    projects, project_sinks, project_repos = tables.projects, tables.project_sinks, tables.project_repos
    project = (
        await conn.execute(
            select(
                projects.c.levels,
                projects.c.locations,
                projects.c.default_label,
                projects.c.cluster,
                projects.c.workspace,
                projects.c.harness_path,
            ).where(projects.c.id == project_id)
        )
    ).one()
    sinks = await conn.execute(
        select(project_sinks.c.sink_id, project_sinks.c.kind, project_sinks.c.clearance).where(
            project_sinks.c.project_id == project_id
        )
    )
    repos = await conn.execute(
        select(
            project_repos.c.name, project_repos.c.origin, project_repos.c.default_branch, project_repos.c.path
        ).where(project_repos.c.project_id == project_id)
    )
    return {
        "levels": project.levels,
        "locations": project.locations,
        "default_label": project.default_label,
        "harness": (project.cluster, project.workspace, project.harness_path),
        "sinks": sorted((sink.sink_id, sink.kind, _clearance(sink.clearance)) for sink in sinks),
        "repos": sorted(tuple(repo) for repo in repos),
    }


async def _write_children(conn: AsyncConnection, project_id: int, desired: dict) -> None:
    project_sinks, project_repos = tables.project_sinks, tables.project_repos
    await conn.execute(delete(project_sinks).where(project_sinks.c.project_id == project_id))
    await conn.execute(delete(project_repos).where(project_repos.c.project_id == project_id))
    sinks = [
        {"project_id": project_id, "sink_id": sink, "kind": kind, "clearance": clearance}
        for sink, kind, clearance in desired["sinks"]
    ]
    if sinks:
        await conn.execute(insert(project_sinks), sinks)
    repos = [
        {"project_id": project_id, "name": name, "origin": origin, "default_branch": branch, "path": path}
        for name, origin, branch, path in desired["repos"]
    ]
    if repos:
        await conn.execute(insert(project_repos), repos)


async def _check_grants(conn: AsyncConnection, name: str, project_id: int, levels: list[str]) -> None:
    """409 when a grant reaches a level the new ladder drops."""
    grants, users = tables.grants, tables.users
    found = await conn.execute(
        select(users.c.login, grants.c.max_level)
        .join_from(grants, users, users.c.id == grants.c.user_id)
        .where(grants.c.project_id == project_id, grants.c.max_level.not_in(levels))
        .order_by(func.lower(users.c.login))
    )
    stranded = found.all()
    if stranded:
        held = ", ".join(f"{login} ({level})" for login, level in stranded)
        raise HTTPException(
            409,
            f"the new levels of project {name} drop levels that grants reach: {held}. Change those grants with "
            "`evo-agents hub admin grant` first",
        )


def _refusal(name: str) -> str:
    return f"registering or updating project {name} needs a hub admin, or the admin role on project {name}"


def _visible(user: Principal, name: str | None):
    """The projects ``user`` holds a grant on (every one for a hub admin), only ``name`` when given, and only the
    run's project for the agent of a run; with the user's grant, by name."""
    projects, grants = tables.projects, tables.grants
    query = select(
        projects.c.id,
        projects.c.name,
        projects.c.cluster,
        projects.c.workspace,
        projects.c.harness_path,
        projects.c.levels,
        projects.c.locations,
        projects.c.default_label,
        projects.c.created_at,
        projects.c.updated_at,
        grants.c.role,
        grants.c.max_level,
    ).select_from(_with_grant(user.user_id))
    if not user.admin:
        query = query.where(grants.c.user_id.is_not(None))
    if name is not None:
        query = query.where(projects.c.name == name)
    if user.run_project is not None:
        query = query.where(projects.c.name == user.run_project)
    return query.order_by(projects.c.name)


async def _projects(conn: AsyncConnection, user: Principal, name: str | None = None) -> list[Project]:
    project_sinks, project_repos = tables.project_sinks, tables.project_repos
    rows = (await conn.execute(_visible(user, name))).all()
    ids = [row.id for row in rows]
    sinks: dict[int, list[Sink]] = {}
    repos: dict[int, list[Repo]] = {}
    held_sinks = await conn.execute(
        select(project_sinks.c.project_id, project_sinks.c.sink_id, project_sinks.c.kind, project_sinks.c.clearance)
        .where(project_sinks.c.project_id.in_(ids))
        .order_by(project_sinks.c.sink_id)
    )
    for sink in held_sinks:
        clearance = Clearance(**sink.clearance)
        sinks.setdefault(sink.project_id, []).append(Sink(id=sink.sink_id, kind=sink.kind, clearance=clearance))
    held_repos = await conn.execute(
        select(
            project_repos.c.project_id,
            project_repos.c.name,
            project_repos.c.origin,
            project_repos.c.default_branch,
            project_repos.c.path,
        )
        .where(project_repos.c.project_id.in_(ids))
        .order_by(project_repos.c.name)
    )
    for repo in held_repos:
        repos.setdefault(repo.project_id, []).append(
            Repo(name=repo.name, origin=repo.origin, default_branch=repo.default_branch, path=repo.path)
        )
    projects = []
    for row in rows:
        harness = Harness(name=row.cluster, workspace=row.workspace, path=row.harness_path) if row.cluster else None
        role, max_level = scoped_grant(user, row.levels, row.role, row.max_level)
        projects.append(
            Project(
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
            )
        )
    return projects


@router.get("", response_model=list[Project])
async def list_projects(request: Request, user: CurrentUser) -> list[Project]:
    async with request.app.state.engine.begin() as conn:
        return await _projects(conn, user)


@router.get("/{project}", response_model=Project, responses={404: {"model": ErrorBody}})
async def show(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    async with request.app.state.engine.begin() as conn:
        found = await _projects(conn, user, project)
    if not found:
        raise HTTPException(404, not_found(project))
    return found[0]


def _locked(name: str):
    """The id of project ``name``, its row locked until the transaction ends."""
    projects = tables.projects
    return select(projects.c.id).where(projects.c.name == name).with_for_update()


@router.put("/{project}", response_model=Registered, responses=REFUSALS)
async def register(request: Request, body: Registration, project: ProjectName, user: CurrentUser) -> Registered:
    """Register ``project`` from its harness, or bring the hub's copy up to date with it."""
    rules = _checked(project, body)
    desired = _desired(body, rules)
    cluster, workspace, harness_path = desired["harness"]
    fields = {
        "levels": desired["levels"],
        "locations": desired["locations"],
        "default_label": desired["default_label"],
        "cluster": cluster,
        "workspace": workspace,
        "harness_path": harness_path,
    }
    projects, grants = tables.projects, tables.grants
    async with request.app.state.engine.begin() as conn:
        project_id = (await conn.execute(_locked(project))).scalar()
        created = False
        if project_id is None:
            if not user.admin:
                raise HTTPException(403, _refusal(project))
            inserted = await conn.execute(
                pg_insert(projects)
                .values(name=project, created_by=user.user_id, **fields)
                .on_conflict_do_nothing(index_elements=[projects.c.name])
                .returning(projects.c.id)
            )
            project_id = inserted.scalar()
            created = project_id is not None
            if not created:  # registered by another request a moment ago: this one updates it
                project_id = (await conn.execute(_locked(project))).scalar_one()
        changed = created
        if not created:
            role = (
                await conn.execute(
                    select(grants.c.role).where(grants.c.user_id == user.user_id, grants.c.project_id == project_id)
                )
            ).scalar()
            role = scoped_grant(user, body.levels, role, None)[0] if role else None  # a run's agent: writer
            if not user.admin and role != "admin":
                raise HTTPException(403, _refusal(project))
            changed = await _held(conn, project_id) != desired
            if changed:
                await _check_grants(conn, project, project_id, body.levels)
                await conn.execute(
                    update(projects).values(**fields, updated_at=func.now()).where(projects.c.id == project_id)
                )
        if changed:
            await _write_children(conn, project_id, desired)
            action = audit.PROJECT_REGISTER if created else audit.PROJECT_UPDATE
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=project)
        (info,) = await _projects(conn, user, project)  # a hub admin or a project admin sees it
    outcome = "registered" if created else "updated" if changed else "unchanged"
    log.info("project registration", extra={"project": project, "outcome": outcome, "login": user.login})
    return Registered(**info.model_dump(), created=created, changed=changed)
