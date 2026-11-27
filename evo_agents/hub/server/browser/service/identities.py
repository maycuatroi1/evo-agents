"""What the browser identities of a project allow: who sees, creates, deletes and sets the default one, which worker
and agent profile an identity may name, and the audit row of every write (``audit.BROWSER_*``).

These deal in metadata alone: no answer, log line or audit row holds a cookie, a profile, a key or a hash of one.
Deleting an identity takes its keys from the row and records its blobs for deletion (``service.profiles``).
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.browser.models.identities import MAX_IDENTITIES, BrowserIdentity
from evo_agents.hub.server.browser.repository import identities as repository
from evo_agents.hub.server.browser.repository.profiles import locked
from evo_agents.hub.server.browser.service.profiles import retire_profiles
from evo_agents.hub.server.browser.service.shapes import BrowserIdentityCreate
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger(__name__)


# Reading


def identity_can_manage(row, user: Principal, access: ProjectAccess) -> bool:
    if row.owner_kind == "member":
        return row.owner_id == user.user_id
    return holds(access.role, "admin")


def identity_view(row, project: str, user: Principal, access: ProjectAccess) -> BrowserIdentity:
    return BrowserIdentity(
        id=row.id,
        project=project,
        name=row.name,
        owner=row.owner_kind,
        owner_login=row.owner_login,
        agent=row.agent_handle,
        agent_default=row.profile_default,
        domains=list(row.domains),
        login_url=row.login_url,
        worker=row.worker_name,
        worker_owner=row.worker_owner_login,
        status=row.status,
        version=row.version,
        size=row.size,
        chromium_version=row.chromium_version,
        held_by_run=row.held_by_run,
        last_saved_at=row.last_saved_at,
        last_saved_by=row.saver_login,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        can_manage=identity_can_manage(row, user, access),
    )


async def shown(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, project: str, identity_id: int
) -> BrowserIdentity:
    """Identity ``identity_id`` of the project (named ``project`` as the caller wrote it) as the caller sees it."""
    row = await repository.listed_one(conn, access.project_id, identity_id)
    return identity_view(row, project, user, access)


async def list_identities(conn: AsyncConnection, user: Principal, project: str) -> list[BrowserIdentity]:
    """The project's identities and the caller's own in it, never another member's, and its agents' for an admin."""
    access = await project_access(conn, user, project)
    agents = holds(access.role, "admin")
    rows = await repository.listed(conn, access.project_id, user.user_id, agents=agents)
    return [identity_view(row, project, user, access) for row in rows]


# Writing


async def identity_worker(conn: AsyncConnection, user: Principal, access: ProjectAccess, name: str) -> int:
    """The id of worker ``name``: the caller's own, not revoked, or a shared one of another member, serving the
    project, whose last heartbeat listed the feature browser; the caller's own when two share the name. 422
    otherwise."""
    rows = await repository.usable_workers(conn, user.user_id, access.project_id, name)
    own = [row for row in rows if row.owner_id == user.user_id]
    if len(own or rows) != 1:
        why = "is shared by more than one member" if rows else "is neither your worker, not revoked, nor a shared one"
        raise Unprocessable(
            f"worker {name} {why} that serves project {access.name}: a browser identity is held by your own worker or "
            "a shared one that serves its project (GET /v1/workers)",
        )
    found = (own or rows)[0]
    if runs.BROWSER not in (found.features or ()):
        raise Unprocessable(
            f"worker {name} does not report the feature browser, so it cannot open a browser identity: run "
            "`evo-agents worker browser install` on it and restart its daemon",
        )
    return found.id


def may_create(access: ProjectAccess, owner: str) -> None:
    needed = "writer" if owner == "member" else "admin"
    if not holds(access.role, needed):
        held = access.role or "no grant"
        whose = "your own" if owner == "member" else f"{'the project' if owner == 'project' else 'an agent'}'s"
        raise Forbidden(
            f"creating {whose} browser identity in project {access.name} needs the {needed} role; you hold {held}"
        )


async def agent_profile_id(conn: AsyncConnection, access: ProjectAccess, handle: str) -> int:
    """The id of the live, non-default agent profile ``handle`` of the project, its row locked; 404 for none, 422 for
    the default profile or a deleted one."""
    found = await repository.agent_profile(conn, access.project_id, handle)
    if found is None:
        raise NotFound(
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`",
        )
    if found.is_default or found.deleted_at is not None:
        why = "the default agent of the project" if found.is_default else "deleted"
        raise Unprocessable(f"@{handle} is {why}: it takes no browser identity")
    return found.id


async def _taken(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, body: BrowserIdentityCreate, profile_id: int | None
) -> None:
    """409 when the owner has a live identity of that name in the project, or the project has MAX_IDENTITIES."""
    if await repository.name_taken(
        conn, access.project_id, body.owner, body.name, user_id=user.user_id, profile_id=profile_id
    ):
        whose = {"project": "the project has", "agent": f"@{body.agent} has"}.get(body.owner, "you have")
        raise Conflict(f"{whose} a browser identity named {body.name} in project {access.name} already")
    count = await repository.live_count(conn, access.project_id)
    if count >= MAX_IDENTITIES:
        raise Conflict(f"project {access.name} has {count} browser identities, the most it may have")


async def identity_record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, identity_id: int):
    await Auditor.of(conn, user, access).record(action, f"{access.name} browser-identity:{identity_id}")


async def insert_identity(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, body: BrowserIdentityCreate, worker_id: int
) -> int:
    """Insert the identity ``body`` describes, held by worker ``worker_id``, and audit it; its id. The first identity of
    an agent profile is the one the profile hands its runs."""
    profile_id = await agent_profile_id(conn, access, body.agent) if body.agent is not None else None
    await _taken(conn, user, access, body, profile_id)
    first = profile_id is not None and not await repository.has_default(conn, profile_id)
    row = {
        "project_id": access.project_id,
        "owner_kind": body.owner,
        "owner_id": user.user_id if body.owner == "member" else None,
        "profile_id": profile_id,
        "profile_default": first,
        "name": body.name,
        "domains": list(dict.fromkeys(body.domains)),
        "login_url": body.login_url,
        "worker_id": worker_id,
        "created_by": user.user_id,
    }
    try:
        identity_id = await repository.inserted(conn, row)
    except IntegrityError:  # another request created one of that name meanwhile
        raise Conflict(f"a browser identity named {body.name} was just created in project {access.name}") from None
    await identity_record(conn, user, access, audit.BROWSER_IDENTITY_CREATE, identity_id)
    return identity_id


def check_create(body: BrowserIdentityCreate) -> None:
    """422 for a login_url that is no address, or an agent that does not go with the owner."""
    if body.login_url is not None and not browser_login.valid_url(body.login_url):
        raise Unprocessable("login_url is an http or https URL with a host; nothing was created")
    if (body.owner == "agent") != (body.agent is not None):
        raise Unprocessable("agent names the agent profile of an identity with owner agent, and only of one")


async def create_identity(
    conn: AsyncConnection, user: Principal, project: str, body: BrowserIdentityCreate
) -> BrowserIdentity:
    """Create the identity ``body`` describes, with no profile yet (version 0); the caller ran ``check_create``."""
    access = await project_access(conn, user, project)
    may_create(access, body.owner)
    worker_id = await identity_worker(conn, user, access, body.worker)
    identity_id = await insert_identity(conn, user, access, body, worker_id)
    created = await shown(conn, user, access, project, identity_id)
    log.info(
        "browser identity created",
        extra={"project": project, "login": user.login, "browser_identity": identity_id, "owner": body.owner},
    )
    return created


async def delete_identity(conn: AsyncConnection, user: Principal, project: str, identity_id: int) -> None:
    """Delete an identity: its member, or an admin for the project's; 409 while a run holds it."""
    access = await project_access(conn, user, project)
    row = await locked(conn, identity_id)
    if (
        row is None
        or row.project_id != access.project_id
        or row.deleted_at is not None
        or (row.owner_kind == "member" and row.owner_id != user.user_id)
    ):
        raise NotFound(f"project {project} has no browser identity {identity_id} of yours or its own")
    if not identity_can_manage(row, user, access):
        held = access.role or "no grant"
        raise Forbidden(f"deleting a browser identity of project {project} needs the admin role on it; you hold {held}")
    if row.held_by_run is not None:
        raise Conflict(f"run {row.held_by_run} holds the browser identity; delete it after the run ends")
    await retire_profiles(conn, row)
    await identity_record(conn, user, access, audit.BROWSER_IDENTITY_DELETE, identity_id)
    log.info(
        "browser identity deleted", extra={"project": project, "login": user.login, "browser_identity": identity_id}
    )


async def make_default(conn: AsyncConnection, user: Principal, project: str, identity_id: int) -> BrowserIdentity:
    """Make an agent's identity the one its profile hands to every run under it: an admin of the project."""
    access = await project_access(conn, user, project)
    row = await locked(conn, identity_id)
    if row is None or row.project_id != access.project_id or row.deleted_at is not None or row.owner_kind != "agent":
        raise NotFound(f"project {project} has no browser identity {identity_id} of an agent")
    if not identity_can_manage(row, user, access):
        held = access.role or "no grant"
        raise Forbidden(
            f"setting the browser identity of an agent of project {project} needs the admin role; you hold {held}",
        )
    await repository.make_default(conn, row.profile_id, identity_id)
    await identity_record(conn, user, access, audit.BROWSER_IDENTITY_DEFAULT, identity_id)
    return await shown(conn, user, access, project, identity_id)


async def retire_profile_identities(conn: AsyncConnection, profile_id: int) -> int:
    """Delete the browser identities of agent profile ``profile_id`` with the profile (their keys go from the hub and
    their profiles from the blob store); how many. 409 while a run holds one."""
    ids = await repository.of_profile(conn, profile_id)
    for identity_id in ids:
        row = await locked(conn, identity_id)
        assert row is not None  # an id of_profile just listed, its row locked
        if row.held_by_run is not None:
            raise Conflict(
                f"run {row.held_by_run} holds the browser identity {row.name} of the agent: delete it after the run ends",
            )
        await retire_profiles(conn, row)
    return len(ids)
