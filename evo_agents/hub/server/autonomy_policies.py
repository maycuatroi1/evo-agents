"""The autonomy policy of a project, kept as revisions (plan gitlab-merge-queue, criterion a2; the keys in
``evo_agents.hub.autonomy``). Table autonomy_policies, schema 0079.

- GET /v1/projects/{project}/policy: the policy now, for a member with a grant on the project: the keys its newest
  revision sets (``policy``) and every key at that value or its default (``effective``), each repo of the project
  included. Revision 0 is a project that never had one: every repo off, as the hub was before the policy.
- GET .../policy/revisions, newest first, each with the keys whose value it changed; GET .../revisions/{revision},
  one of them whole; GET .../policy/compare?from=N&to=M, the keys whose value differs between two revisions.
- PUT .../policy: the next revision, for an admin of the project. The body is the whole policy, as the admin writes
  it; a body equal to the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision
  now is 409. The agent of a run never writes it: @builder proposes a change (proposal kind ``policy``,
  ``evo_agents.core_plugins.builder.proposals``), which an admin approves, and the hub writes it through ``write``.

Writes of one project's policy wait for each other (a transaction-level advisory lock). Each revision adds an audit
row ``policy.set`` naming the revision and the keys whose value it changed, never a value. ``effective_policy`` is
what the hub's own jobs read.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_REVISION = 2**31 - 1
MAX_AUDITED_KEYS_CHARS = 1000
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
RevisionNumber = Annotated[int, Path(ge=1, le=MAX_REVISION)]

router = APIRouter(prefix="/v1/projects", tags=["policy"], responses={401: {"model": ErrorBody}})


class AutonomyPolicy(BaseModel):
    """One revision of a project's autonomy policy."""

    project: str
    revision: int = Field(description="0 while the project has none: every repo off, every key at its default")
    policy: dict[str, Any] = Field(description="the keys the revision sets, as its writer wrote them")
    effective: dict[str, Any] = Field(
        description="every key at the value the revision sets or its default, each repo of the project included"
    )
    note: str | None = Field(description="what the writer said of it")
    created_by: str | None
    created_at: datetime | None


class PolicyWritten(AutonomyPolicy):
    changed: bool = Field(description="false when the newest revision is this policy already: nothing was written")


class PolicyWrite(BaseModel):
    """The next revision of the policy: the whole of it, as its writer wants it."""

    model_config = ConfigDict(extra="forbid")

    policy: dict[str, Any] = Field(
        description="sections repos, merge, judge, breaker, audit; a key left out reads as its default"
    )
    note: str | None = Field(None, max_length=autonomy.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(None, ge=0, description="409 unless the policy is at this revision now")


class PolicyRevision(BaseModel):
    revision: int
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the dotted keys whose value differs from the revision before")


class PolicyChange(BaseModel):
    key: str = Field(description="dotted: merge.hold_until, repos.<repo>.mode, repos.<repo>.jobs.uat_check")
    before: Any
    after: Any


class PolicyComparison(BaseModel):
    project: str
    from_revision: int
    to_revision: int
    changes: list[PolicyChange] = Field(description="each key whose value differs, as the two revisions read now")


# Reading


def _revisions():
    a, u = tables.autonomy_policies, tables.users
    return select(a.c.revision, a.c.body, a.c.note, u.c.login.label("created_by"), a.c.created_at).join_from(
        a, u, u.c.id == a.c.created_by
    )


async def newest(conn: AsyncConnection, project_id: int):
    """The newest revision of the project's policy (revision, body, note, created_by, created_at); None without one."""
    a = tables.autonomy_policies
    query = _revisions().where(a.c.project_id == project_id).order_by(a.c.revision.desc()).limit(1)
    return (await conn.execute(query)).first()


async def _at(conn: AsyncConnection, access: ProjectAccess, revision: int):
    """Revision ``revision`` of the project's policy; None for revision 0; 404 when it has no such revision."""
    if revision == 0:
        return None
    a = tables.autonomy_policies
    query = _revisions().where(a.c.project_id == access.project_id, a.c.revision == revision)
    row = (await conn.execute(query)).first()
    if row is None:
        raise HTTPException(404, f"the policy of project {access.name} has no revision {revision}")
    return row


async def body_at(conn: AsyncConnection, project_id: int, revision: int) -> dict | None:
    """What revision ``revision`` of the project's policy sets: {} for revision 0, None when there is no such one."""
    if revision == 0:
        return {}
    a = tables.autonomy_policies
    query = select(a.c.body).where(a.c.project_id == project_id, a.c.revision == revision)
    return (await conn.execute(query)).scalar_one_or_none()


async def project_repos(conn: AsyncConnection, project_id: int) -> dict[str, autonomy.RepoFacts]:
    """The repos the project lists, with their origin and default branch."""
    r = tables.project_repos
    query = select(r.c.name, r.c.origin, r.c.default_branch).where(r.c.project_id == project_id)
    return {row.name: autonomy.RepoFacts(row.origin, row.default_branch) for row in await conn.execute(query)}


async def effective_policy(conn: AsyncConnection, project_id: int) -> dict:
    """The project's policy now, every key at its value or its default (``autonomy.effective``)."""
    row = await newest(conn, project_id)
    return autonomy.effective(None if row is None else row.body, await project_repos(conn, project_id))


def _view(project: str, row, repos: dict[str, autonomy.RepoFacts]) -> AutonomyPolicy:
    if row is None:
        effective = autonomy.effective(None, repos)
        return AutonomyPolicy(
            project=project, revision=0, policy={}, effective=effective, note=None, created_by=None, created_at=None
        )
    return AutonomyPolicy(
        project=project,
        revision=row.revision,
        policy=row.body,
        effective=autonomy.effective(row.body, repos),
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


async def view_at(conn: AsyncConnection, access: ProjectAccess, revision: int | None = None) -> AutonomyPolicy:
    """The project's policy at ``revision``, the newest when None."""
    row = await newest(conn, access.project_id) if revision is None else await _at(conn, access, revision)
    return _view(access.name, row, await project_repos(conn, access.project_id))


# Writing


def refusal(user: Principal, access: ProjectAccess) -> str | None:
    """Why ``user`` may not write the project's policy; None when they may."""
    if user.scope is not None:
        return "the agent of a run never writes the policy: @builder proposes a change, which an admin approves"
    if has_role(access.role, "admin"):
        return None
    held = access.role or "no grant"
    return f"writing the policy of project {access.name} needs the admin role on it; {user.login} holds {held}"


async def _lock(conn: AsyncConnection, project_id: int) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the project's policy."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"autonomy_policy:{project_id}", 0))))


def _keys_text(keys: list[str]) -> str:
    text = ",".join(keys)
    return text if len(text) <= MAX_AUDITED_KEYS_CHARS else text[: MAX_AUDITED_KEYS_CHARS - 3] + "..."


async def write(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    body: dict,
    *,
    note: str | None = None,
    if_revision: int | None = None,
    via: str = "",
) -> tuple[int, bool]:
    """Write ``body`` as the next revision of the project's policy, once it is right (422 naming each problem
    otherwise) and while the policy is at ``if_revision`` (409 otherwise); (the revision now, whether one was
    written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked ``refusal``."""
    await _lock(conn, access.project_id)
    held = await newest(conn, access.project_id)
    now = 0 if held is None else held.revision
    if if_revision is not None and if_revision != now:
        raise HTTPException(
            409, f"the policy of project {access.name} is at revision {now}, not {if_revision}; nothing changed"
        )
    repos = await project_repos(conn, access.project_id)
    problems = autonomy.problems(body, repos)
    if problems:
        raise HTTPException(422, f"{'; '.join(problems)}; nothing changed")
    before = {} if held is None else held.body
    if body == before:
        return now, False
    keys = [key for key, _, _ in autonomy.changes(autonomy.effective(before, repos), autonomy.effective(body, repos))]
    values = {
        "project_id": access.project_id,
        "revision": now + 1,
        "body": body,
        "note": " ".join((note or "").split()) or None,
        "created_by": user.user_id,
    }
    await conn.execute(insert(tables.autonomy_policies).values(values))
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.POLICY_SET,
        target=f"{access.name}/policy revision={now + 1} keys={_keys_text(keys)}{via}",
        project_id=access.project_id,
    )
    log.info("autonomy policy written", extra={"project": access.name, "revision": now + 1, "keys": len(keys)})
    return now + 1, True


# Routes


async def _reader(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the policy of project {access.name} needs a grant on it")
    return access


@router.get("/{project}/policy", response_model=AutonomyPolicy, responses=REFUSALS)
async def show_policy(request: Request, project: ProjectName, user: CurrentUser) -> AutonomyPolicy:
    """The project's policy now: what its newest revision sets, and every key as it reads."""
    async with request.app.state.engine.begin() as conn:
        return await view_at(conn, await _reader(conn, user, project))


@router.put("/{project}/policy", response_model=PolicyWritten, responses=WRITE_REFUSALS)
async def write_policy(request: Request, project: ProjectName, body: PolicyWrite, user: CurrentUser) -> PolicyWritten:
    """Write the project's policy whole as its next revision; an admin of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        refused = refusal(user, access)
        if refused is not None:
            raise HTTPException(403, refused)
        revision, changed = await write(conn, user, access, body.policy, note=body.note, if_revision=body.if_revision)
        shown = await view_at(conn, access, revision)
    return PolicyWritten(**shown.model_dump(), changed=changed)


@router.get("/{project}/policy/revisions", response_model=list[PolicyRevision], responses=REFUSALS)
async def policy_revisions(request: Request, project: ProjectName, user: CurrentUser) -> list[PolicyRevision]:
    """Every revision of the project's policy, the newest first, each with the keys whose value it changed."""
    a = tables.autonomy_policies
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        query = _revisions().where(a.c.project_id == access.project_id).order_by(a.c.revision)
        rows = (await conn.execute(query)).all()
        repos = await project_repos(conn, access.project_id)
    listed, before = [], autonomy.effective(None, repos)
    for row in rows:
        after = autonomy.effective(row.body, repos)
        changed = [key for key, _, _ in autonomy.changes(before, after)]
        listed.append(
            PolicyRevision(
                revision=row.revision,
                note=row.note,
                created_by=row.created_by,
                created_at=row.created_at,
                changed=changed,
            )
        )
        before = after
    return listed[::-1]


@router.get("/{project}/policy/revisions/{revision}", response_model=AutonomyPolicy, responses=REFUSALS)
async def show_policy_revision(
    request: Request, project: ProjectName, revision: RevisionNumber, user: CurrentUser
) -> AutonomyPolicy:
    """One revision of the project's policy whole."""
    async with request.app.state.engine.begin() as conn:
        return await view_at(conn, await _reader(conn, user, project), revision)


@router.get("/{project}/policy/compare", response_model=PolicyComparison, responses=REFUSALS)
async def compare_policy(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    from_revision: Annotated[
        int, Query(alias="from", ge=0, le=MAX_REVISION, description="the older revision; 0 for no policy")
    ],
    to_revision: Annotated[
        int | None, Query(alias="to", ge=0, le=MAX_REVISION, description="the newer revision; the newest when left out")
    ] = None,
) -> PolicyComparison:
    """The keys whose value differs between two revisions of the project's policy, each as it reads now."""
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        before = await view_at(conn, access, from_revision)
        after = await view_at(conn, access, to_revision)
    changes = [
        PolicyChange(key=key, before=old, after=new)
        for key, old, new in autonomy.changes(before.effective, after.effective)
    ]
    return PolicyComparison(
        project=access.name, from_revision=before.revision, to_revision=after.revision, changes=changes
    )
