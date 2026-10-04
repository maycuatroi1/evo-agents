"""Skills: versions of skill directories whose bundles live in the blob store, for ``hub skills sync`` to write into
the skills directories of every runtime.

A skill is global (every signed-in member reads it, a hub admin publishes it) or belongs to a project (the members
of the project read it; publishing follows the write rule of ``evo_agents.hub.access``: the writer role, and a hub
sink that clears the project's default label). Asking about a project skill without a grant on the project is a
403, the same whether the project exists or not; a hub admin without a grant reads nothing there either. Names are
unique in their scope ignoring case, since a name is a directory name on every machine.

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
the SHA-256 the client checks the bytes against. Routes exist twice: under /v1/skills/global/ and under
/v1/skills/projects/{project}/.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import BLOB_PREFIX, GET_TTL, BlobStoreUnavailable
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


async def _readable(conn, user: Principal, project: str | None) -> Place:
    """The place of ``project``'s skills (global without one) when ``user`` may read them; 403 otherwise, the same
    for a project that does not exist."""
    if project is None:
        return GLOBAL_PLACE
    cursor = await conn.execute(
        "SELECT p.id FROM projects p JOIN grants g ON g.project_id = p.id AND g.user_id = %s WHERE p.name = %s",
        (user.user_id, project),
    )
    row = await cursor.fetchone()
    if row is None:
        raise _no_grant(project)
    return Place("project", row[0], project)


async def _writable(conn, user: Principal, project: str | None) -> Place:
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


SKILLS = """
SELECT s.scope, p.name, s.name, v.version, v.description, v.sha256, v.size, v.source_repo, v.source_commit, u.login,
       v.published_at
  FROM skills s
  LEFT JOIN projects p ON p.id = s.project_id
  JOIN LATERAL (SELECT * FROM skill_versions WHERE skill_id = s.id ORDER BY version DESC LIMIT 1) v ON true
  JOIN users u ON u.id = v.published_by
 WHERE (s.scope = 'global' AND %(global)s)
    OR (s.scope = 'project' AND s.project_id = ANY(%(projects)s))
 ORDER BY s.scope, p.name NULLS FIRST, lower(s.name)
"""


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
    async with request.app.state.pool.connection() as conn:
        if project is not None:
            projects = [(await _readable(conn, user, project)).project_id]
        else:
            cursor = await conn.execute("SELECT project_id FROM grants WHERE user_id = %s", (user.user_id,))
            projects = [row[0] for row in await cursor.fetchall()]
        wanted = {
            "global": scope in (None, "global") and project is None,
            "projects": projects if scope in (None, "project") else [],
        }
        rows = await (await conn.execute(SKILLS, wanted)).fetchall()
    fields = Skill.model_fields
    return [Skill(**dict(zip(fields, row, strict=True))) for row in rows]


VERSIONS = """
SELECT v.version, v.description, v.sha256, v.size, v.source_repo, v.source_commit, u.login, v.published_at
  FROM skill_versions v JOIN users u ON u.id = v.published_by
 WHERE v.skill_id = %s ORDER BY v.version DESC
"""
FIND = """
SELECT id, name, created_at FROM skills
 WHERE scope = %s AND project_id IS NOT DISTINCT FROM %s AND lower(name) = lower(%s)
"""


async def _find(conn, place: Place, name: str, *, lock: bool = False):
    """(id, name, created_at) of skill ``name`` in ``place``, matched ignoring case; None when there is none."""
    cursor = await conn.execute(FIND + (" FOR UPDATE" if lock else ""), (place.scope, place.project_id, name))
    return await cursor.fetchone()


def _not_found(place: Place, name: str) -> HTTPException:
    where = "global skill" if place.project is None else f"skill of project {place.project}"
    return HTTPException(404, f"no {where} {name}: see `evo-agents hub skills list`")


async def _history(request: Request, user: Principal, project: str | None, name: str) -> SkillHistory:
    async with request.app.state.pool.connection() as conn:
        place = await _readable(conn, user, project)
        found = await _find(conn, place, name)
        if found is None:
            raise _not_found(place, name)
        rows = await (await conn.execute(VERSIONS, (found[0],))).fetchall()
    versions = [Version(**dict(zip(Version.model_fields, row, strict=True))) for row in rows]
    return SkillHistory(scope=place.scope, project=place.project, name=found[1], created_at=found[2], versions=versions)


@router.get("/global/{name}", response_model=SkillHistory, responses=READ_REFUSALS)
async def global_skill(request: Request, name: SkillName, user: CurrentUser) -> SkillHistory:
    """A global skill and every version of it."""
    return await _history(request, user, None, name)


@router.get("/projects/{project}/{name}", response_model=SkillHistory, responses=READ_REFUSALS)
async def project_skill(request: Request, project: ProjectName, name: SkillName, user: CurrentUser) -> SkillHistory:
    """A skill of a project and every version of it."""
    return await _history(request, user, project, name)


# Publishing

COMMITTED = "SELECT size FROM blobs WHERE project_id IS NOT DISTINCT FROM %s AND sha256 = %s"
LATEST = "SELECT version, sha256 FROM skill_versions WHERE skill_id = %s ORDER BY version DESC LIMIT 1"
INSERT_SKILL = """
INSERT INTO skills (scope, project_id, name, created_by) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id
"""
INSERT_VERSION = """
INSERT INTO skill_versions (skill_id, version, name, description, sha256, size, r2_key, source_repo, source_commit,
                            published_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


async def _committed(conn, place: Place, body: PublishRequest) -> None:
    """422 unless the place's holder has committed the bundle with the declared size."""
    holder = GLOBAL if place.project is None else f"project {place.project}"
    row = await (await conn.execute(COMMITTED, (place.project_id, body.sha256))).fetchone()
    if row is None:
        raise HTTPException(
            422,
            f"bundle {body.sha256} is not committed for {holder}: ask for an upload (POST /v1/blobs/uploads), PUT it "
            "and commit it (POST /v1/blobs/commit) first; no version was created",
        )
    if row[0] != body.size:
        raise HTTPException(
            422, f"bundle {body.sha256} has {row[0]} bytes, not the {body.size} declared; no version was created"
        )


async def _latest(conn, skill_id: int) -> Version:
    cursor = await conn.execute(VERSIONS + " LIMIT 1", (skill_id,))
    return Version(**dict(zip(Version.model_fields, await cursor.fetchone(), strict=True)))


async def _publish(request: Request, user: Principal, project: str | None, name: str, body: PublishRequest):
    name = _checked_name(name)
    store = blob_store(request)
    pool = request.app.state.pool
    async with pool.connection() as conn:
        place = await _writable(conn, user, project)
        await _committed(conn, place, body)
        found = await _find(conn, place, name)
        if found is not None:
            if found[1] != name:
                raise HTTPException(422, f"the {place.describe()} have {found[1]}, a name differing only in case")
            latest = await (await conn.execute(LATEST, (found[0],))).fetchone()
            if latest is not None and latest[1] == body.sha256:
                return Published(
                    scope=place.scope,
                    project=place.project,
                    name=name,
                    created=False,
                    latest=await _latest(conn, found[0]),
                )
    try:
        data = await asyncio.to_thread(store.fetch, body.sha256, MAX_BUNDLE)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    except ValueError:
        data = None
    if data is None or hashlib.sha256(data).hexdigest() != body.sha256:
        log.error("a committed bundle is missing or altered in the blob store", extra={"sha256": body.sha256})
        raise HTTPException(
            503, "the blob store does not hold this bundle as committed; no version was created, tell a hub admin"
        )
    try:
        contents = read_bundle(data, name)
    except BundleError as exc:
        raise HTTPException(422, f"bundle {body.sha256} is not a bundle of skill {name}: {exc}") from None
    async with pool.connection() as conn:
        place = await _writable(conn, user, project)  # the grant may have changed while the bundle was read
        await _committed(conn, place, body)
        cursor = await conn.execute(INSERT_SKILL, (place.scope, place.project_id, name, user.user_id))
        await cursor.fetchone()
        skill_id, held, _ = await _find(conn, place, name, lock=True)  # the version numbers follow one at a time
        if held != name:
            raise HTTPException(422, f"the {place.describe()} have {held}, a name differing only in case")
        latest = await (await conn.execute(LATEST, (skill_id,))).fetchone()
        created = latest is None or latest[1] != body.sha256
        if created:
            await conn.execute(
                INSERT_VERSION,
                (
                    skill_id,
                    (latest[0] if latest else 0) + 1,
                    contents.name,
                    contents.description[:MAX_DESCRIPTION],
                    body.sha256,
                    body.size,
                    BLOB_PREFIX + body.sha256,
                    body.source_repo,
                    body.source_commit,
                    user.user_id,
                ),
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

BUNDLE = """
SELECT v.version, v.sha256, v.size FROM skill_versions v
 WHERE v.skill_id = %s AND (%s::integer IS NULL OR v.version = %s) ORDER BY v.version DESC LIMIT 1
"""


async def _bundle(request: Request, user: Principal, project: str | None, name: str, version: int | None):
    store = blob_store(request)
    async with request.app.state.pool.connection() as conn:
        place = await _readable(conn, user, project)
        found = await _find(conn, place, name)
        row = None if found is None else await (await conn.execute(BUNDLE, (found[0], version, version))).fetchone()
    if row is None:
        if found is not None and version is not None:
            raise HTTPException(404, f"{found[1]} has no version {version}")
        raise _not_found(place, name)
    number, sha256, size = row
    expires_at = datetime.now(timezone.utc) + GET_TTL  # taken before signing, so never later than the URL
    url = await asyncio.to_thread(store.presign_get, sha256)
    log.info(
        "skill bundle handed out",
        extra={"scope": place.scope, "project": place.project, "skill": found[1], "version": number},
    )
    return BundleTicket(
        scope=place.scope,
        project=place.project,
        name=found[1],
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
