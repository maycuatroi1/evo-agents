"""Skills: versions of skill directories whose bundles live in the blob store, for ``hub skills sync`` to write into
the skills directories of every runtime.

A skill is global (every signed-in member reads it, a hub admin publishes it) or belongs to a project (the members
of the project read it; publishing follows the write rule of ``evo_agents.hub.access``: the writer role, and a hub
sink that clears the project's default label). Asking about a project skill without a grant on the project is a
403, the same whether the project exists or not; a hub admin without a grant reads nothing there either, and the
agent of a run (``Principal.scope``) reads the global skills and those of the run's project alone. Names are unique in
their scope ignoring case, since a name is a directory name on every machine.

A version records the bundle's SHA-256, size and key (blobs/sha256/<sha256>), the name and description of its
SKILL.md, who published it and when, and optionally the repo and commit it was built from. The bytes stay in the
blob store. Publishing happens in four calls: POST /v1/blobs/uploads for the bundle (kind skill-bundle, held by the
project, or by the hub itself for a global skill; over 10 MiB is refused there before any URL is issued), the PUT to
the presigned URL, POST /v1/blobs/commit, then POST .../versions here. That last call creates a version only for a
bundle the holder has committed, so the hub read the bytes back and hashed them, and only after reading the bundle
again from the blob store and checking it as ``evo_agents.hub.skills.read_bundle`` does (a well-formed tar.gz whose
SKILL.md names this skill); a bundle not committed, or another SHA-256 or size, is a 422 that creates nothing. The
same bundle as the latest version changes nothing. Every new version adds one audit row naming the skill,
``skill:global/<name>`` or ``skill:project/<project>/<name>``, never its content.

GET /v1/skills lists the skills the caller sees with their latest version; GET .../{name} is one skill with every
version; GET .../{name}/bundle hands out a presigned GET of one version's bundle, after the read check, together with
the SHA-256 the client checks the bytes against. The URL makes the blob store answer as a download named
``<name>-v<version>.tar.gz``, so a browser sent to it saves the file (the web navigates there and never fetches it,
so the bucket needs no CORS). The read check comes before the blob store is asked for: without a grant the answer is
403 whether the store is configured or not. Routes exist twice: under /v1/skills/global/ and under
/v1/skills/projects/{project}/.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Select, and_, func, insert, or_, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import BLOB_PREFIX, GET_TTL, BlobStoreUnavailable, blob_key
from evo_agents.hub.server.admin import PROJECT_NAME, ProjectName
from evo_agents.hub.server.audit import record
from evo_agents.hub.server.blobs import GLOBAL, blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import PRINTABLE, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.skills import MAX_BUNDLE, MAX_DESCRIPTION, BundleError, name_problem, read_bundle

log = logging.getLogger(__name__)

PUBLISH = "skill.publish"  # audit action; the target is skill:global/<name> or skill:project/<project>/<name>
SCOPES = ("global", "project")
MAX_SOURCE_REPO = 200
SKILL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"  # evo_agents.hub.skills.NAME
UNAVAILABLE = "the blob store did not answer; no version was created, try again shortly"

SkillName = Annotated[str, Path(pattern=SKILL_NAME, description="the skill's name, its directory in every runtime")]
VersionNumber = Annotated[int | None, Query(ge=1, le=2**31 - 1, description="default: the latest")]
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
PUBLISH_REFUSALS = {code: {"model": ErrorBody} for code in (403, 422, 503)}
BUNDLE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 503)}

router = APIRouter(prefix="/v1/skills", tags=["skills"], responses={401: {"model": ErrorBody}})


class PublishRequest(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="the bundle, committed to the blob store already")
    size: int = Field(ge=1, le=MAX_BUNDLE, description="bytes of the bundle")
    source_repo: str | None = Field(None, min_length=1, max_length=MAX_SOURCE_REPO, pattern=PRINTABLE)
    source_commit: str | None = Field(None, pattern=r"^[0-9a-f]{7,64}$", description="hex, abbreviated or whole")

    @model_validator(mode="after")
    def _source_whole(self):
        if (self.source_repo is None) != (self.source_commit is None):
            raise ValueError("source_repo and source_commit go together")
        return self


class Version(BaseModel):
    version: int
    description: str = Field(description="from the frontmatter of SKILL.md")
    sha256: str
    size: int
    source_repo: str | None
    source_commit: str | None
    published_by: str
    published_at: datetime


class Skill(BaseModel):
    """A skill with its latest version."""

    scope: Literal[SCOPES]
    project: str | None
    name: str
    version: int
    description: str
    sha256: str
    size: int
    source_repo: str | None
    source_commit: str | None
    published_by: str
    published_at: datetime


class SkillHistory(BaseModel):
    scope: Literal[SCOPES]
    project: str | None
    name: str
    created_at: datetime
    versions: list[Version] = Field(description="the latest first")


class Published(BaseModel):
    scope: Literal[SCOPES]
    project: str | None
    name: str
    created: bool = Field(description="false when the latest version has this bundle already")
    latest: Version


class BundleTicket(BaseModel):
    scope: Literal[SCOPES]
    project: str | None
    name: str
    version: int
    sha256: str = Field(description="what the downloaded bytes must hash to")
    size: int
    url: str = Field(description="presigned GET of the bundle; a bearer credential until it expires")
    expires_at: datetime


@dataclass(frozen=True)
class Place:
    """Where a skill lives: global, or a project the caller has a grant on."""

    scope: str
    project_id: int | None
    project: str | None

    def target(self, name: str) -> str:
        """The skill ``name`` here, as audit rows name it."""
        return f"skill:global/{name}" if self.project is None else f"skill:project/{self.project}/{name}"

    def describe(self) -> str:
        return "global skills" if self.project is None else f"skills of project {self.project}"


GLOBAL_PLACE = Place("global", None, None)


def _no_grant(project: str) -> HTTPException:
    return HTTPException(
        403, f"skills of project {project} are for its members: ask an admin of the project for a grant"
    )


async def _readable(conn: AsyncConnection, user: Principal, project: str | None) -> Place:
    """The place of ``project``'s skills (global without one) when ``user`` may read them; 403 otherwise, the same
    for a project that does not exist."""
    if project is None:
        return GLOBAL_PLACE
    if not user.reaches(project):  # the agent of a run reads the skills of the run's project alone
        raise _no_grant(project)
    projects, grants = tables.projects, tables.grants
    granted = (
        select(projects.c.id)
        .join_from(projects, grants, and_(grants.c.project_id == projects.c.id, grants.c.user_id == user.user_id))
        .where(projects.c.name == project)
    )
    row = (await conn.execute(granted)).first()
    if row is None:
        raise _no_grant(project)
    return Place("project", row.id, project)


async def _writable(conn: AsyncConnection, user: Principal, project: str | None) -> Place:
    """The place ``user`` publishes into: global for a hub admin, a project under its write rule. 403 or 422
    otherwise, before anything is written."""
    if project is None:
        if not user.admin:
            raise HTTPException(403, "publishing a global skill needs a hub admin; a member publishes into a project")
        return GLOBAL_PLACE
    try:
        access = await project_access(conn, user, project)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise _no_grant(project) from None
        raise
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"publishing skills of project {project} needs the writer role on it")
    access.push_label()  # the write rule: a hub sink that clears the project's default label
    return Place("project", access.project_id, project)


def _checked_name(name: str) -> str:
    problem = name_problem(name)
    if problem:
        raise HTTPException(422, problem)
    return name


def _skills(with_global: bool, project_ids: list[int]) -> Select:
    """The skills of Skill, column by field name, each with its latest version: the global ones with
    ``with_global``, and those of the projects ``project_ids``."""
    skills, projects, users, versions = tables.skills, tables.projects, tables.users, tables.skill_versions
    latest = (
        select(versions)
        .where(versions.c.skill_id == skills.c.id)
        .order_by(versions.c.version.desc())
        .limit(1)
        .lateral("v")
    )
    wanted = [and_(skills.c.scope == "project", skills.c.project_id.in_(project_ids))]
    if with_global:
        wanted.insert(0, skills.c.scope == "global")
    return (
        select(
            skills.c.scope,
            projects.c.name.label("project"),
            skills.c.name,
            latest.c.version,
            latest.c.description,
            latest.c.sha256,
            latest.c.size,
            latest.c.source_repo,
            latest.c.source_commit,
            users.c.login.label("published_by"),
            latest.c.published_at,
        )
        .select_from(
            skills.outerjoin(projects, projects.c.id == skills.c.project_id)
            .join(latest, true())
            .join(users, users.c.id == latest.c.published_by)
        )
        .where(or_(*wanted))
        .order_by(skills.c.scope, projects.c.name.nulls_first(), func.lower(skills.c.name))
    )


def _granted(user: Principal) -> Select:
    """The projects whose skills a caller lists: those it holds a grant on, the run's alone for the agent of a run."""
    grants, projects = tables.grants, tables.projects
    query = (
        select(grants.c.project_id)
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .where(grants.c.user_id == user.user_id)
    )
    if user.run_project is not None:
        query = query.where(projects.c.name == user.run_project)
    return query


@router.get("", response_model=list[Skill], responses={403: {"model": ErrorBody}})
async def list_skills(
    request: Request,
    user: CurrentUser,
    scope: Annotated[Literal[SCOPES] | None, Query(description="default: both")] = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME, description="only this project's skills")] = None,
) -> list[Skill]:
    """The skills the caller sees, each with its latest version: the global ones and those of every project the
    caller has a grant on."""
    if project is not None and scope == "global":
        raise HTTPException(422, "a global skill belongs to no project: pass scope=project with a project, or neither")
    async with request.app.state.engine.begin() as conn:
        if project is not None:
            projects = [(await _readable(conn, user, project)).project_id]
        else:
            projects = list((await conn.execute(_granted(user))).scalars())
        with_global = scope in (None, "global") and project is None
        rows = (await conn.execute(_skills(with_global, projects if scope in (None, "project") else []))).all()
    return [Skill(**row._mapping) for row in rows]


def _versions(skill_id: int) -> Select:
    """The versions of skill ``skill_id`` with what Version shows, column by field name, the latest first."""
    versions, users = tables.skill_versions, tables.users
    return (
        select(
            versions.c.version,
            versions.c.description,
            versions.c.sha256,
            versions.c.size,
            versions.c.source_repo,
            versions.c.source_commit,
            users.c.login.label("published_by"),
            versions.c.published_at,
        )
        .join_from(versions, users, users.c.id == versions.c.published_by)
        .where(versions.c.skill_id == skill_id)
        .order_by(versions.c.version.desc())
    )


async def _find(conn: AsyncConnection, place: Place, name: str, *, lock: bool = False):
    """(id, name, created_at) of skill ``name`` in ``place``, matched ignoring case; None when there is none."""
    skills = tables.skills
    query = select(skills.c.id, skills.c.name, skills.c.created_at).where(
        skills.c.scope == place.scope,
        skills.c.project_id.is_not_distinct_from(place.project_id),
        func.lower(skills.c.name) == func.lower(name),
    )
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).first()


def _not_found(place: Place, name: str) -> HTTPException:
    where = "global skill" if place.project is None else f"skill of project {place.project}"
    return HTTPException(404, f"no {where} {name}: see `evo-agents hub skills list`")


async def _history(request: Request, user: Principal, project: str | None, name: str) -> SkillHistory:
    async with request.app.state.engine.begin() as conn:
        place = await _readable(conn, user, project)
        found = await _find(conn, place, name)
        if found is None:
            raise _not_found(place, name)
        rows = (await conn.execute(_versions(found.id))).all()
    versions = [Version(**row._mapping) for row in rows]
    return SkillHistory(
        scope=place.scope, project=place.project, name=found.name, created_at=found.created_at, versions=versions
    )


@router.get("/global/{name}", response_model=SkillHistory, responses=READ_REFUSALS)
async def global_skill(request: Request, name: SkillName, user: CurrentUser) -> SkillHistory:
    """A global skill and every version of it."""
    return await _history(request, user, None, name)


@router.get("/projects/{project}/{name}", response_model=SkillHistory, responses=READ_REFUSALS)
async def project_skill(request: Request, project: ProjectName, name: SkillName, user: CurrentUser) -> SkillHistory:
    """A skill of a project and every version of it."""
    return await _history(request, user, project, name)


# Publishing


def _latest_bundle(skill_id: int) -> Select:
    """The number and SHA-256 of the latest version of skill ``skill_id``."""
    versions = tables.skill_versions
    query = select(versions.c.version, versions.c.sha256).where(versions.c.skill_id == skill_id)
    return query.order_by(versions.c.version.desc()).limit(1)


async def _committed(conn: AsyncConnection, place: Place, body: PublishRequest) -> None:
    """422 unless the place's holder has committed the bundle with the declared size."""
    holder = GLOBAL if place.project is None else f"project {place.project}"
    blobs = tables.blobs
    held = select(blobs.c.size).where(
        blobs.c.project_id.is_not_distinct_from(place.project_id), blobs.c.sha256 == body.sha256
    )
    row = (await conn.execute(held)).first()
    if row is None:
        raise HTTPException(
            422,
            f"bundle {body.sha256} is not committed for {holder}: ask for an upload (POST /v1/blobs/uploads), PUT it "
            "and commit it (POST /v1/blobs/commit) first; no version was created",
        )
    if row.size != body.size:
        raise HTTPException(
            422, f"bundle {body.sha256} has {row.size} bytes, not the {body.size} declared; no version was created"
        )


async def _latest(conn: AsyncConnection, skill_id: int) -> Version:
    return Version(**(await conn.execute(_versions(skill_id).limit(1))).one()._mapping)


async def _publish(request: Request, user: Principal, project: str | None, name: str, body: PublishRequest):
    name = _checked_name(name)
    store = blob_store(request)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        place = await _writable(conn, user, project)
        await _committed(conn, place, body)
        found = await _find(conn, place, name)
        if found is not None:
            if found.name != name:
                raise HTTPException(422, f"the {place.describe()} have {found.name}, a name differing only in case")
            latest = (await conn.execute(_latest_bundle(found.id))).first()
            if latest is not None and latest.sha256 == body.sha256:
                return Published(
                    scope=place.scope,
                    project=place.project,
                    name=name,
                    created=False,
                    latest=await _latest(conn, found.id),
                )
    bundle = io.BytesIO()
    try:
        fetched = await asyncio.to_thread(store.fetch, blob_key(body.sha256), bundle, MAX_BUNDLE)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if fetched != (body.sha256, body.size):  # None when missing; a size over MAX_BUNDLE when larger
        log.error("a committed bundle is missing or altered in the blob store", extra={"sha256": body.sha256})
        raise HTTPException(
            503, "the blob store does not hold this bundle as committed; no version was created, tell a hub admin"
        )
    try:
        contents = read_bundle(bundle.getvalue(), name)
    except BundleError as exc:
        raise HTTPException(422, f"bundle {body.sha256} is not a bundle of skill {name}: {exc}") from None
    async with engine.begin() as conn:
        place = await _writable(conn, user, project)  # the grant may have changed while the bundle was read
        await _committed(conn, place, body)
        skills = tables.skills
        new_skill = pg_insert(skills).values(
            scope=place.scope, project_id=place.project_id, name=name, created_by=user.user_id
        )
        await conn.execute(new_skill.on_conflict_do_nothing().returning(skills.c.id))
        skill_id, held, _ = await _find(conn, place, name, lock=True)  # the version numbers follow one at a time
        if held != name:
            raise HTTPException(422, f"the {place.describe()} have {held}, a name differing only in case")
        latest = (await conn.execute(_latest_bundle(skill_id))).first()
        created = latest is None or latest.sha256 != body.sha256
        if created:
            await conn.execute(
                insert(tables.skill_versions).values(
                    skill_id=skill_id,
                    version=(latest.version if latest else 0) + 1,
                    name=contents.name,
                    description=contents.description[:MAX_DESCRIPTION],
                    sha256=body.sha256,
                    size=body.size,
                    r2_key=BLOB_PREFIX + body.sha256,
                    source_repo=body.source_repo,
                    source_commit=body.source_commit,
                    published_by=user.user_id,
                )
            )
            target = place.target(name)
            await record(conn, actor_id=user.user_id, token_id=user.token_id, action=PUBLISH, target=target)
        version = await _latest(conn, skill_id)
    log.info(
        "skill published" if created else "skill unchanged",
        extra={"scope": place.scope, "project": place.project, "skill": name, "version": version.version},
    )
    return Published(scope=place.scope, project=place.project, name=name, created=created, latest=version)


@router.post("/global/{name}/versions", response_model=Published, responses=PUBLISH_REFUSALS)
async def publish_global(request: Request, name: SkillName, body: PublishRequest, user: CurrentUser) -> Published:
    """A new version of a global skill from a committed bundle (a hub admin only)."""
    return await _publish(request, user, None, name, body)


@router.post("/projects/{project}/{name}/versions", response_model=Published, responses=PUBLISH_REFUSALS)
async def publish_project(
    request: Request, project: ProjectName, name: SkillName, body: PublishRequest, user: CurrentUser
) -> Published:
    """A new version of a skill of a project from a bundle the project committed."""
    return await _publish(request, user, project, name, body)


# Downloading


def _bundle_of(skill_id: int, version: int | None) -> Select:
    """The number, SHA-256 and size of version ``version`` of skill ``skill_id``, the latest without one."""
    versions = tables.skill_versions
    query = select(versions.c.version, versions.c.sha256, versions.c.size).where(versions.c.skill_id == skill_id)
    if version is not None:
        query = query.where(versions.c.version == version)
    return query.order_by(versions.c.version.desc()).limit(1)


async def _bundle(request: Request, user: Principal, project: str | None, name: str, version: int | None):
    async with request.app.state.engine.begin() as conn:
        place = await _readable(conn, user, project)
        found = await _find(conn, place, name)
        row = None if found is None else (await conn.execute(_bundle_of(found.id, version))).first()
    if row is None:
        if found is not None and version is not None:
            raise HTTPException(404, f"{found.name} has no version {version}")
        raise _not_found(place, name)
    store = blob_store(request)  # after the read check: who may not read the skill learns nothing of the store
    number, sha256, size = row
    expires_at = datetime.now(UTC) + GET_TTL  # taken before signing, so never later than the URL
    filename = f"{found.name}-v{number}.tar.gz"  # a skill name is [A-Za-z0-9._-], safe in a header as it is
    url = await asyncio.to_thread(store.presign_get, sha256, filename=filename)
    log.info(
        "skill bundle handed out",
        extra={"scope": place.scope, "project": place.project, "skill": found.name, "version": number},
    )
    return BundleTicket(
        scope=place.scope,
        project=place.project,
        name=found.name,
        version=number,
        sha256=sha256,
        size=size,
        url=url,
        expires_at=expires_at,
    )


@router.get("/global/{name}/bundle", response_model=BundleTicket, responses=BUNDLE_REFUSALS)
async def global_bundle(request: Request, name: SkillName, user: CurrentUser, version: VersionNumber = None):
    """A short-lived presigned GET of a global skill's bundle, with the SHA-256 to check it against."""
    return await _bundle(request, user, None, name, version)


@router.get("/projects/{project}/{name}/bundle", response_model=BundleTicket, responses=BUNDLE_REFUSALS)
async def project_bundle(
    request: Request, project: ProjectName, name: SkillName, user: CurrentUser, version: VersionNumber = None
):
    """A short-lived presigned GET of a project skill's bundle, for its members only (403 for anyone else)."""
    return await _bundle(request, user, project, name, version)
