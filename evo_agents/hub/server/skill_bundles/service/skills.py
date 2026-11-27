"""Reading, publishing and handing out skills: who may, what the blob store is asked, and what the routes of
``evo_agents.hub.server.skills`` answer."""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.blobs import BLOB_PREFIX, GET_TTL, BlobStore, BlobStoreUnavailable, blob_key
from evo_agents.hub.server.audit import record
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blob_store.models.uploads import GLOBAL
from evo_agents.hub.server.blob_store.service.uploads import configured
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.skill_bundles.models.skills import (
    AUDIENCE,
    AUDIENCES,
    EVERYONE,
    GLOBAL_PLACE,
    PUBLISH,
    UNAVAILABLE,
    BundleTicket,
    Place,
    Published,
    PublishRequest,
    Skill,
    SkillAudience,
    SkillHistory,
    Version,
)
from evo_agents.hub.server.skill_bundles.repository import skills as repo
from evo_agents.hub.skills import MAX_BUNDLE, MAX_DESCRIPTION, BundleError, name_problem, read_bundle

log = logging.getLogger("evo_agents.hub.server.skills")


def audiences(config, user: Principal) -> tuple[str, ...]:
    """The audiences of the global skills ``user`` reads: every one for a hub admin and for the agent of a run whose
    owner is one (the agent is never a hub admin itself, ``Principal.admin``, but reads what its owner reads);
    everyone's alone for anyone else."""
    if user.admin or (user.scope is not None and config.is_admin(user.admin_login)):
        return AUDIENCES
    return (EVERYONE,)


def _no_grant(project: str) -> HTTPException:
    return HTTPException(
        403, f"skills of project {project} are for its members: ask an admin of the project for a grant"
    )


async def _readable(conn: AsyncConnection, config, user: Principal, project: str | None) -> Place:
    """The place of ``project``'s skills (global without one, with the audiences ``user`` reads) when ``user`` may read
    them; 403 otherwise, the same for a project that does not exist."""
    if project is None:
        return Place("global", None, None, audiences(config, user))
    if not user.reaches(project):  # the agent of a run reads the skills of the run's project alone
        raise _no_grant(project)
    project_id = await repo.granted_project(conn, user.user_id, project)
    if project_id is None:
        raise _no_grant(project)
    return Place("project", project_id, project)


async def writable(conn: AsyncConnection, user: Principal, project: str | None) -> Place:
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
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"publishing skills of project {project} needs the writer role on it")
    access.push_label()  # the write rule: a hub sink that clears the project's default label
    return Place("project", access.project_id, project)


def _checked_name(name: str) -> str:
    problem = name_problem(name)
    if problem:
        raise HTTPException(422, problem)
    return name


async def list_skills(
    engine: AsyncEngine, config, user: Principal, scope: str | None, project: str | None, page: Paging
) -> list[Skill]:
    """What GET /v1/skills answers: the skills ``user`` sees, each with its latest version."""
    if project is not None and scope == "global":
        raise HTTPException(422, "a global skill belongs to no project: pass scope=project with a project, or neither")
    async with UnitOfWork(engine) as conn:
        if project is not None:
            projects = [(await _readable(conn, config, user, project)).project_id]
        else:
            projects = await repo.granted_ids(conn, user)
        with_global = audiences(config, user) if scope in (None, "global") and project is None else ()
        wanted = projects if scope in (None, "project") else []
        rows = await repo.latest_rows(conn, page, with_global, wanted)  # pyright: ignore[reportArgumentType]
    return [Skill(**row._mapping) for row in rows]


def _not_found(place: Place, name: str) -> HTTPException:
    where = "global skill" if place.project is None else f"skill of project {place.project}"
    return HTTPException(404, f"no {where} {name}: see `evo-agents hub skills list`")


async def history(engine: AsyncEngine, config, user: Principal, project: str | None, name: str) -> SkillHistory:
    """A skill and every version of it, as GET .../{name} answers."""
    async with UnitOfWork(engine) as conn:
        place = await _readable(conn, config, user, project)
        found = await repo.find(conn, place, name)
        if found is None:
            raise _not_found(place, name)
        rows = await repo.versions(conn, found.id)
    versions = [Version(**row._mapping) for row in rows]
    return SkillHistory(
        scope=place.scope,
        project=place.project,
        name=found.name,
        created_at=found.created_at,
        versions=versions,
        audience=found.audience,
    )


# Publishing


async def _committed(conn: AsyncConnection, place: Place, body: PublishRequest) -> None:
    """422 unless the place's holder has committed the bundle with the declared size."""
    holder = GLOBAL if place.project is None else f"project {place.project}"
    row = await repo.committed(conn, place, body.sha256)
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
    return Version(**(await repo.latest_version(conn, skill_id))._mapping)


async def _audience(conn: AsyncConnection, user: Principal, place: Place, found, wanted: str | None) -> str:
    """The audience of skill ``found`` (a row of ``repo.find``) once set to ``wanted``, adding one audit row when that
    changes it; unchanged without ``wanted``."""
    if wanted is None or wanted == found.audience:
        return found.audience
    await repo.set_audience(conn, found.id, wanted)
    target = f"{place.target(found.name)} audience={found.audience}->{wanted}"
    await record(conn, actor_id=user.user_id, token_id=user.token_id, action=AUDIENCE, target=target)
    log.info("skill audience changed", extra={"skill": found.name, "audience": wanted})
    return wanted


async def _unchanged(engine: AsyncEngine, user: Principal, project: str | None, name: str, body: PublishRequest):
    """The answer of a publication whose bundle is the latest version's already; None when it is not."""
    async with UnitOfWork(engine) as conn:
        place = await writable(conn, user, project)
        await _committed(conn, place, body)
        found = await repo.find(conn, place, name, lock=body.audience is not None)
        if found is not None:
            if found.name != name:
                raise HTTPException(422, f"the {place.describe()} have {found.name}, a name differing only in case")
            latest = await repo.latest_bundle(conn, found.id)
            if latest is not None and latest.sha256 == body.sha256:
                return Published(
                    scope=place.scope,
                    project=place.project,
                    name=name,
                    created=False,
                    latest=await _latest(conn, found.id),
                    audience=await _audience(conn, user, place, found, body.audience),
                )
    return None


async def _contents(store: BlobStore, name: str, body: PublishRequest):
    """What the bundle of ``body`` holds, read back from the blob store and checked as a bundle of skill ``name``."""
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
        return read_bundle(bundle.getvalue(), name)
    except BundleError as exc:
        raise HTTPException(422, f"bundle {body.sha256} is not a bundle of skill {name}: {exc}") from None


async def publish(
    engine: AsyncEngine, blobs: BlobStore | None, config, user: Principal, project: str | None, name: str, body
) -> Published:
    """A new version of skill ``name`` of ``project`` (None: global) from the committed bundle of ``body``, as POST
    .../{name}/versions answers; ``blobs`` and ``config`` are the hub's blob store and configuration."""
    name = _checked_name(name)
    if project is not None and body.audience not in (None, EVERYONE):
        raise HTTPException(
            422, f"a skill of a project is for its members: audience {body.audience} is a global skill's"
        )
    store = configured(blobs, config)
    unchanged = await _unchanged(engine, user, project, name, body)
    if unchanged is not None:
        return unchanged
    contents = await _contents(store, name, body)
    async with UnitOfWork(engine) as conn:
        place = await writable(conn, user, project)  # the grant may have changed while the bundle was read
        await _committed(conn, place, body)
        await repo.add_skill(conn, place, name, user.user_id, body.audience or EVERYONE)
        found = await repo.find(conn, place, name, lock=True)  # the version numbers follow one at a time
        skill_id, held = found.id, found.name  # pyright: ignore[reportOptionalMemberAccess]
        if held != name:
            raise HTTPException(422, f"the {place.describe()} have {held}, a name differing only in case")
        audience = await _audience(conn, user, place, found, body.audience)
        latest = await repo.latest_bundle(conn, skill_id)
        created = latest is None or latest.sha256 != body.sha256
        if created:
            await repo.add_version(
                conn,
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
            target = place.target(name)
            await record(conn, actor_id=user.user_id, token_id=user.token_id, action=PUBLISH, target=target)
        version = await _latest(conn, skill_id)
    log.info(
        "skill published" if created else "skill unchanged",
        extra={"scope": place.scope, "project": place.project, "skill": name, "version": version.version},
    )
    return Published(
        scope=place.scope, project=place.project, name=name, created=created, latest=version, audience=audience
    )


async def set_audience(engine: AsyncEngine, user: Principal, name: str, wanted: str) -> SkillAudience:
    """Who reads global skill ``name`` once set to ``wanted``, as PUT /v1/skills/global/{name}/audience answers."""
    async with UnitOfWork(engine) as conn:
        found = await repo.find(conn, GLOBAL_PLACE, name, lock=True)
        if found is None:
            raise _not_found(GLOBAL_PLACE, name)
        audience = await _audience(conn, user, GLOBAL_PLACE, found, wanted)
    return SkillAudience(name=found.name, audience=audience, changed=found.audience != audience)


# Downloading


async def bundle(
    engine: AsyncEngine,
    blobs: BlobStore | None,
    config,
    user: Principal,
    project: str | None,
    name: str,
    version: int | None,
) -> BundleTicket:
    """A short-lived presigned GET of version ``version`` (None: the latest) of skill ``name`` of ``project`` (None:
    global), as GET .../{name}/bundle answers."""
    async with UnitOfWork(engine) as conn:
        place = await _readable(conn, config, user, project)
        found = await repo.find(conn, place, name)
        row = None if found is None else await repo.bundle_of(conn, found.id, version)
    if row is None:
        if found is not None and version is not None:
            raise HTTPException(404, f"{found.name} has no version {version}")
        raise _not_found(place, name)
    store = configured(blobs, config)  # after the read check: who may not read the skill learns nothing of the store
    number, sha256, size = row
    expires_at = datetime.now(UTC) + GET_TTL  # taken before signing, so never later than the URL
    # a skill name is [A-Za-z0-9._-], safe in a header as it is
    filename = f"{found.name}-v{number}.tar.gz"  # pyright: ignore[reportOptionalMemberAccess]
    url = await asyncio.to_thread(store.presign_get, sha256, filename=filename)
    log.info(
        "skill bundle handed out",
        extra={"scope": place.scope, "project": place.project, "skill": found.name, "version": number},  # pyright: ignore[reportOptionalMemberAccess]
    )
    return BundleTicket(
        scope=place.scope,
        project=place.project,
        name=found.name,  # pyright: ignore[reportOptionalMemberAccess]
        version=number,
        sha256=sha256,
        size=size,
        url=url,
        expires_at=expires_at,
    )


async def _pool_node(conn: AsyncConnection, config, user: Principal) -> Principal:
    """The reader of the global skills for the worker whose token made the request: its owner, a hub admin when the
    owner is one. 403 for a worker that is not a live node of a pool."""
    pool_id = await repo.live_pool(conn, user.token_id)
    if pool_id is None:
        raise HTTPException(403, "the skills of the hub are synced on the nodes of a worker pool alone")
    return replace(user, admin=config.is_admin(user.admin_login))


async def worker_skills(engine: AsyncEngine, config, user: Principal, page: Paging) -> list[Skill]:
    """The global skills the owner of the pool node ``user`` reads, each with its latest version."""
    async with UnitOfWork(engine) as conn:
        owner = await _pool_node(conn, config, user)
        rows = await repo.latest_rows(conn, page, audiences(config, owner), [])
    return [Skill(**row._mapping) for row in rows]


async def worker_bundle(
    engine: AsyncEngine, blobs: BlobStore | None, config, user: Principal, name: str, version: int | None
) -> BundleTicket:
    """A presigned GET of a global skill's bundle for the pool node ``user``, as its owner gets it."""
    async with UnitOfWork(engine) as conn:
        owner = await _pool_node(conn, config, user)
    return await bundle(engine, blobs, config, owner, None, name, version)
