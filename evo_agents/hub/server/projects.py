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
admin without a grant manages a project but reads and pushes nothing in it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from evo_agents.harness import load_schema
from evo_agents.hub.access import HUB_KIND, INTEGRITIES, ProjectRules, Refused
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


async def _one(conn, query: str, params=()):
    return await (await conn.execute(query, params)).fetchone()


async def project_access(conn, user: Principal, name: str) -> ProjectAccess:
    """``user``'s access to project ``name``; 404 when it is not registered, or when ``user`` holds no grant on it
    and is no hub admin, so a name tells nothing about the projects one cannot see."""
    row = await _one(
        conn,
        "SELECT p.id, p.levels, p.locations, p.default_label, g.role, g.max_level FROM projects p "
        "LEFT JOIN grants g ON g.project_id = p.id AND g.user_id = %s WHERE p.name = %s",
        (user.user_id, name),
    )
    if row is None or (row[4] is None and not user.admin):
        raise HTTPException(404, not_found(name))
    project_id, levels, locations, default_label, role, max_level = row
    cursor = await conn.execute(
        "SELECT sink_id, kind, clearance FROM project_sinks WHERE project_id = %s ORDER BY sink_id", (project_id,)
    )
    sinks = [{"id": sink, "kind": kind, "clearance": clearance} for sink, kind, clearance in await cursor.fetchall()]
    return ProjectAccess(project_id, ProjectRules(name, levels, locations, sinks, default_label), role, max_level)


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


async def _held(conn, project_id: int) -> dict:
    levels, locations, default_label, cluster, workspace, harness_path = await _one(
        conn,
        "SELECT levels, locations, default_label, cluster, workspace, harness_path FROM projects WHERE id = %s",
        (project_id,),
    )
    sinks = await (
        await conn.execute("SELECT sink_id, kind, clearance FROM project_sinks WHERE project_id = %s", (project_id,))
    ).fetchall()
    repos = await (
        await conn.execute(
            "SELECT name, origin, default_branch, path FROM project_repos WHERE project_id = %s", (project_id,)
        )
    ).fetchall()
    return {
        "levels": levels,
        "locations": locations,
        "default_label": default_label,
        "harness": (cluster, workspace, harness_path),
        "sinks": sorted((sink, kind, _clearance(clearance)) for sink, kind, clearance in sinks),
        "repos": sorted(tuple(repo) for repo in repos),
    }


async def _write_children(conn, project_id: int, desired: dict) -> None:
    await conn.execute("DELETE FROM project_sinks WHERE project_id = %s", (project_id,))
    await conn.execute("DELETE FROM project_repos WHERE project_id = %s", (project_id,))
    async with conn.cursor() as cursor:
        await cursor.executemany(
            "INSERT INTO project_sinks (project_id, sink_id, kind, clearance) VALUES (%s, %s, %s, %s)",
            [(project_id, sink, kind, Jsonb(clearance)) for sink, kind, clearance in desired["sinks"]],
        )
        await cursor.executemany(
            "INSERT INTO project_repos (project_id, name, origin, default_branch, path) VALUES (%s, %s, %s, %s, %s)",
            [(project_id, *repo) for repo in desired["repos"]],
        )


async def _check_grants(conn, name: str, project_id: int, levels: list[str]) -> None:
    """409 when a grant reaches a level the new ladder drops."""
    cursor = await conn.execute(
        "SELECT u.login, g.max_level FROM grants g JOIN users u ON u.id = g.user_id "
        "WHERE g.project_id = %s AND NOT (g.max_level = ANY(%s)) ORDER BY lower(u.login)",
        (project_id, levels),
    )
    stranded = await cursor.fetchall()
    if stranded:
        held = ", ".join(f"{login} ({level})" for login, level in stranded)
        raise HTTPException(
            409,
            f"the new levels of project {name} drop levels that grants reach: {held}. Change those grants with "
            "`evo-agents hub admin grant` first",
        )


def _refusal(name: str) -> str:
    return f"registering or updating project {name} needs a hub admin, or the admin role on project {name}"


PROJECTS = """
SELECT p.id, p.name, p.cluster, p.workspace, p.harness_path, p.levels, p.locations, p.default_label, p.created_at,
       p.updated_at, g.role, g.max_level
  FROM projects p LEFT JOIN grants g ON g.project_id = p.id AND g.user_id = %(user)s
 WHERE (g.user_id IS NOT NULL OR %(admin)s) AND (%(name)s::text IS NULL OR p.name = %(name)s)
 ORDER BY p.name
"""


async def _projects(conn, user: Principal, name: str | None = None) -> list[Project]:
    rows = await (await conn.execute(PROJECTS, {"user": user.user_id, "admin": user.admin, "name": name})).fetchall()
    ids = [row[0] for row in rows]
    sinks: dict[int, list[Sink]] = {}
    repos: dict[int, list[Repo]] = {}
    cursor = await conn.execute(
        "SELECT project_id, sink_id, kind, clearance FROM project_sinks WHERE project_id = ANY(%s) ORDER BY sink_id",
        (ids,),
    )
    for project_id, sink, kind, clearance in await cursor.fetchall():
        sinks.setdefault(project_id, []).append(Sink(id=sink, kind=kind, clearance=Clearance(**clearance)))
    cursor = await conn.execute(
        "SELECT project_id, name, origin, default_branch, path FROM project_repos WHERE project_id = ANY(%s) "
        "ORDER BY name",
        (ids,),
    )
    for project_id, repo, origin, branch, path in await cursor.fetchall():
        repos.setdefault(project_id, []).append(Repo(name=repo, origin=origin, default_branch=branch, path=path))
    projects = []
    for row in rows:
        project_id, name, cluster, workspace, harness_path, levels, locations, label, created, updated = row[:10]
        harness = Harness(name=cluster, workspace=workspace, path=harness_path) if cluster else None
        projects.append(
            Project(
                name=name,
                harness=harness,
                levels=levels,
                locations=locations,
                default_label=label,
                sinks=sinks.get(project_id, []),
                repos=repos.get(project_id, []),
                role=row[10],
                max_level=row[11],
                created_at=created,
                updated_at=updated,
            )
        )
    return projects


@router.get("", response_model=list[Project])
async def list_projects(request: Request, user: CurrentUser) -> list[Project]:
    async with request.app.state.pool.connection() as conn:
        return await _projects(conn, user)


@router.get("/{project}", response_model=Project, responses={404: {"model": ErrorBody}})
async def show(request: Request, project: ProjectName, user: CurrentUser) -> Project:
    async with request.app.state.pool.connection() as conn:
        found = await _projects(conn, user, project)
    if not found:
        raise HTTPException(404, not_found(project))
    return found[0]


INSERT_PROJECT = """
INSERT INTO projects (name, levels, locations, default_label, cluster, workspace, harness_path, created_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING RETURNING id
"""
UPDATE_PROJECT = """
UPDATE projects SET levels = %s, locations = %s, default_label = %s, cluster = %s, workspace = %s,
       harness_path = %s, updated_at = now()
 WHERE id = %s
"""


@router.put("/{project}", response_model=Registered, responses=REFUSALS)
async def register(request: Request, body: Registration, project: ProjectName, user: CurrentUser) -> Registered:
    """Register ``project`` from its harness, or bring the hub's copy up to date with it."""
    rules = _checked(project, body)
    desired = _desired(body, rules)
    fields = (desired["levels"], desired["locations"], Jsonb(desired["default_label"]), *desired["harness"])
    async with request.app.state.pool.connection() as conn:
        row = await _one(conn, "SELECT id FROM projects WHERE name = %s FOR UPDATE", (project,))
        created = False
        if row is None:
            if not user.admin:
                raise HTTPException(403, _refusal(project))
            row = await _one(conn, INSERT_PROJECT, (project, *fields, user.user_id))
            created = row is not None
            if not created:  # registered by another request a moment ago: this one updates it
                row = await _one(conn, "SELECT id FROM projects WHERE name = %s FOR UPDATE", (project,))
        project_id = row[0]
        changed = created
        if not created:
            grant = await _one(
                conn, "SELECT role FROM grants WHERE user_id = %s AND project_id = %s", (user.user_id, project_id)
            )
            if not user.admin and (grant is None or grant[0] != "admin"):
                raise HTTPException(403, _refusal(project))
            changed = await _held(conn, project_id) != desired
            if changed:
                await _check_grants(conn, project, project_id, body.levels)
                await conn.execute(UPDATE_PROJECT, (*fields, project_id))
        if changed:
            await _write_children(conn, project_id, desired)
            action = audit.PROJECT_REGISTER if created else audit.PROJECT_UPDATE
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=project)
        (info,) = await _projects(conn, user, project)  # a hub admin or a project admin sees it
    outcome = "registered" if created else "updated" if changed else "unchanged"
    log.info("project registration", extra={"project": project, "outcome": outcome, "login": user.login})
    return Registered(**info.model_dump(), created=created, changed=changed)
