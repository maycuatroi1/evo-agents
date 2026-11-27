"""The agent profiles of a project (``evo_agents.hub.profiles``): GET and POST /v1/projects/{p}/agents, GET, PATCH and
DELETE .../agents/{handle}, GET .../agents/{handle}/versions, and PUT, DELETE and GET .../agents/{handle}/avatar.

Every member of the project reads its profiles, soft-deleted ones included, since an old message keeps its author's
name and avatar; someone without a grant gets the 404 of ``project_access``, a hub admin without one a 403. Creating,
editing and deleting a profile, and its avatar, need the admin role on the project: a writer or a reader gets 403.

A handle is unique on the hub, deleted profiles' included, and may not be the name of a project (409): @<project> is
that project's default profile, which the registration of the project created and which can never be deleted; its
permissions stay all and its instructions empty (422), while its name, description, colour, starters and avatar may
change. A handle never changes. A profile attaches agent packs of plugins its project enabled, each pinned at a
version of its plugin (``agent_packs.pinned``; the default profile attaches none). Its starters are at most
``profiles.MAX_STARTERS`` questions of one line each, which the chat home offers (``chats.service.home``). ``runs_on`` says where its runs run, worker
(the default) or hub; the hub only stores it so far. Every edit that
changes something, the avatar and the packs included, raises the version by one and writes the version row; PATCH with
``if_version`` other than the version now is 409, nothing changed. Deleting sets ``deleted_at``: the profile takes no
new run, and reads as before; its accounts (``evo_agents.hub.server.agent_accounts``) are deleted in the same
transaction.

The avatar is a PNG, JPEG or WebP image of at most AVATAR_MAX_BYTES, sent as base64 in JSON and checked by its first
bytes. The hub stores it in the blob store as a blob of kind agent-avatar that the project holds, and serves it back
from GET .../avatar, whose URL (``icon_url``) carries a part of its SHA-256, so a browser may keep it. A profile
without one has ``icon_url`` null: the web draws its initial on its colour.

Each change adds one audit row in the project: agent.create, agent.update (naming the fields that changed) or
agent.delete, its target ``<project>/@<handle> v<version>``, never a description, instructions or an image.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, profiles
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server import audit
from evo_agents.hub.server.accounts.service import pools as account_pools
from evo_agents.hub.server.agent_accounts.service.accounts import retire_profile_accounts
from evo_agents.hub.server.agents.models.packs import pinned_view
from evo_agents.hub.server.agents.models.profiles import (
    AVATAR_KIND,
    AVATAR_MAX_BYTES,
    AVATAR_TYPES,
    UNAVAILABLE,
    AccountPoolChoice,
    Agent,
    AgentCreate,
    AgentEdit,
    AgentPermissions,
    AgentVersion,
    AvatarUpload,
)
from evo_agents.hub.server.agents.repository import profiles as repository
from evo_agents.hub.server.agents.service.packs import pinned
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.blobs import blob_store, fetch_bytes, put_bytes
from evo_agents.hub.server.browser.service.identities import retire_profile_identities
from evo_agents.hub.server.profile_store import Profile, create_pool, icon_url, write_version
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal

# Reading


def agent_view(profile: Profile) -> Agent:
    return Agent(
        handle=profile.handle,
        project=profile.project,
        name=profile.name,
        description=profile.description,
        instructions=profile.instructions,
        color=profile.color,
        icon_url=icon_url(profile),
        avatar_sha256=profile.avatar_sha256,
        starters=list(profile.starters or ()),
        runs_on=profile.runs_on,
        is_default=profile.is_default,
        permissions=AgentPermissions.model_validate(profile.permissions),
        restricted=profiles.restricted_axes(profile.permissions),
        packs=pinned_view(profile.packs),
        home_pool=profile.home_pool,
        account_pool=profile.account_pool,
        version=profile.version,
        created_by=profile.created_by,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        deleted_at=profile.deleted_at,
    )


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise Forbidden(f"reading the agents of project {access.name} needs a grant on it")


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise Forbidden(f"{doing} the agents of project {access.name} needs the admin role on it; you hold {held}")


async def _profile(conn: AsyncConnection, access: ProjectAccess, handle: str, *, lock: bool = False) -> Profile:
    """The profile ``handle`` of the project; 404 when the project has none."""
    found = await repository.profile_row(conn, access.project_id, handle, lock=lock)
    if found is None:
        raise NotFound(
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`"
        )
    return found


async def _readable(conn: AsyncConnection, user: Principal, project: str, handle: str) -> Profile:
    access = await project_access(conn, user, project)
    _reader(access)
    return await _profile(conn, access, handle)


async def list_agents(conn: AsyncConnection, user: Principal, project: str, deleted: bool) -> list[Agent]:
    """The agent profiles of the project, the default first, then by handle."""
    access = await project_access(conn, user, project)
    _reader(access)
    return [agent_view(found) for found in await repository.project_profiles(conn, access.project_id, deleted)]


async def show(conn: AsyncConnection, user: Principal, project: str, handle: str) -> Agent:
    """One agent profile of the project, deleted or not."""
    return agent_view(await _readable(conn, user, project, handle))


async def versions(conn: AsyncConnection, user: Principal, project: str, handle: str) -> list[AgentVersion]:
    """Every version of the profile, the latest first: the rules a run of that version had."""
    found = await _readable(conn, user, project, handle)
    return [AgentVersion(**row._mapping) for row in await repository.versions(conn, found.id)]


# Changing


def _target(profile: Profile, version: int | None = None) -> str:
    return f"{profile.project}/@{profile.handle} v{profile.version if version is None else version}"


def _deleted(profile: Profile, handle: str) -> None:
    if profile.deleted_at is not None:
        raise Conflict(f"@{handle} was deleted at {profile.deleted_at.isoformat()}: it changes no more")


async def create_profile(
    conn: AsyncConnection, user, access: ProjectAccess, body: AgentCreate, hub_version: str
) -> Profile:
    """Create agent profile ``body`` of the project of ``access`` at version 1 as ``user``, with its pool and its audit
    row, in the caller's transaction; the caller checked that ``user`` may (the admin role on the project). 409 when
    the handle is taken or names a project."""
    values = {
        "handle": body.handle,
        "name": body.name,
        "description": body.description,
        "instructions": body.instructions,
        "color": body.color or profiles.default_color(body.handle),
        "avatar_sha256": None,
        "starters": body.starters,
        "runs_on": body.runs_on or profiles.DEFAULT_RUNS_ON,
        "is_default": False,
        "permissions": body.permissions.stored(),
    }
    if await repository.project_named(conn, body.handle):
        raise Conflict(
            f"@{body.handle} is the name of project {body.handle}, whose default agent it names: pick another handle; "
            "nothing was created"
        )
    values["packs"] = await pinned(conn, access.project_id, access.name, body.packs, hub_version)
    profile_id = await repository.insert_profile(conn, access.project_id, user.user_id, values)
    if profile_id is None:
        raise Conflict(f"@{body.handle} is taken by another agent, deleted ones included: pick another handle")
    await write_version(conn, profile_id, 1, values, user.user_id)
    await create_pool(conn, profile_id, body.handle, access.project_id, user.user_id)
    created = await _profile(conn, access, body.handle)
    await Auditor.of(conn, user, access).record(audit.AGENT_CREATE, _target(created))
    return created


async def create(conn: AsyncConnection, user: Principal, project: str, body: AgentCreate, hub_version: str) -> Agent:
    """Create an agent profile of the project, at version 1."""
    access = await project_access(conn, user, project)
    _admin(access, "creating")
    return agent_view(await create_profile(conn, user, access, body, hub_version))


def profile_changes(profile: Profile, body: AgentEdit, packs: list[dict] | None) -> dict:
    """The columns ``body`` changes, each to its new value, its ``packs`` as ``agent_packs.pinned`` checked them; 422
    for what a default profile keeps."""
    wanted = body.model_dump(exclude_unset=True, exclude={"if_version", "permissions", "packs"})
    if body.permissions is not None:
        wanted["permissions"] = body.permissions.stored()
    if packs is not None:
        wanted["packs"] = packs
    if profile.is_default:
        if wanted.get("permissions", profiles.all_access()) != profiles.all_access():
            raise Unprocessable(
                f"@{profile.handle} is the default agent of project {profile.project}: its permissions stay all, so a "
                "run that names no agent runs as before; create another agent to limit them"
            )
        if wanted.get("instructions") or wanted.get("packs"):
            raise Unprocessable(
                f"@{profile.handle} is the default agent of project {profile.project}: it has no instructions and "
                "attaches no agent pack, so a run that names no agent runs as before; create another agent to give "
                "it some"
            )
    current = profile.snapshot()
    return {key: value for key, value in wanted.items() if value is not None and current[key] != value}


async def new_version(conn: AsyncConnection, profile: Profile, changes: dict, user_id: int) -> int:
    """Write ``changes`` to ``profile`` as its next version, with its version row; the new version."""
    version = profile.version + 1
    await repository.write_profile(conn, profile.id, changes, version)
    await write_version(conn, profile.id, version, {**profile.snapshot(), **changes}, user_id)
    return version


async def edit(
    conn: AsyncConnection, user: Principal, project: str, handle: str, body: AgentEdit, hub_version: str
) -> tuple[Agent, dict]:
    """Change an agent profile: the fields given, as its next version; the profile, and the columns that changed."""
    access = await project_access(conn, user, project)
    _admin(access, "editing")
    profile = await _profile(conn, access, handle, lock=True)
    _deleted(profile, handle)
    if body.if_version is not None and body.if_version != profile.version:
        raise Conflict(
            f"@{handle} is at version {profile.version}, not {body.if_version}: read it again; nothing changed"
        )
    packs = await pinned(conn, access.project_id, project, body.packs, hub_version)
    changes = profile_changes(profile, body, packs)
    if changes:
        version = await new_version(conn, profile, changes, user.user_id)
        fields = ",".join(sorted(changes))
        await Auditor.of(conn, user, access).record(audit.AGENT_UPDATE, f"{_target(profile, version)} fields={fields}")
        profile = await _profile(conn, access, handle)
    return agent_view(profile), changes


async def set_account_pool(
    conn: AsyncConnection, user: Principal, project: str, handle: str, body: AccountPoolChoice
) -> Agent:
    """Attach an account pool of the project to the agent, or detach it (an admin of the project or of the hub)."""
    access = await project_access(conn, user, project)
    if not user.admin:
        _admin(access, "attaching an account pool to")
    profile = await _profile(conn, access, handle, lock=True)
    _deleted(profile, handle)
    if profile.account_pool != body.account_pool:
        await account_pools.attach(conn, user, access, profile.id, handle, body.account_pool)
        profile = await _profile(conn, access, handle)
    return agent_view(profile)


async def delete(conn: AsyncConnection, user: Principal, project: str, handle: str) -> tuple[Agent, int]:
    """Soft-delete an agent profile, its accounts and browser identities with it; the profile, and how many accounts
    were deleted."""
    retired = 0
    access = await project_access(conn, user, project)
    _admin(access, "deleting")
    profile = await _profile(conn, access, handle, lock=True)
    if profile.is_default:
        raise Conflict(f"@{handle} is the default agent of project {project}, which is never deleted")
    if profile.deleted_at is None:
        await repository.mark_deleted(conn, profile.id)
        retired = await retire_profile_accounts(conn, profile.id)
        await retire_profile_identities(conn, profile.id)
        await Auditor.of(conn, user, access).record(audit.AGENT_DELETE, _target(profile))
        profile = await _profile(conn, access, handle)
    return agent_view(profile), retired


# The avatar


def media_type(data: bytes) -> str | None:
    """The media type of an avatar's bytes by their first ones, PNG, JPEG or WebP; None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image(body: AvatarUpload) -> tuple[bytes, str]:
    """The bytes of the avatar ``body`` and their SHA-256; 413 when too big, 422 when no image the hub takes."""
    data = base64.b64decode(body.data, validate=True)
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(413, f"the image is {len(data)} bytes, over the {AVATAR_MAX_BYTES} an avatar may have")
    if media_type(data) is None:
        raise Unprocessable(f"an avatar is a {', '.join(AVATAR_TYPES.values())} image, and this one is none")
    return data, hashlib.sha256(data).hexdigest()


async def may_change_avatar(conn: AsyncConnection, user: Principal, project: str) -> None:
    """403 unless ``user`` may change the avatars of the project's agents (the admin role on it)."""
    _admin(await project_access(conn, user, project), "changing the avatar of")


async def _hold_avatar(conn: AsyncConnection, store, access: ProjectAccess, user_id: int, sha256: str, data: bytes):
    """Record that the project holds blob ``sha256``, put back first if a deletion was pending for it."""
    await blob_gc.lock_shared(conn)
    if await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
    await repository.hold_blob(conn, access.project_id, sha256, len(data), AVATAR_KIND, user_id)


async def set_avatar(request: Request, project: str, handle: str, user, sha256: str | None, data: bytes = b"") -> Agent:
    """Set the profile's avatar to blob ``sha256`` (put in the blob store first), or drop it for None, as its next
    version; in a transaction of its own."""
    store = blob_store(request) if sha256 is not None else None
    if store is not None:
        try:
            await asyncio.to_thread(put_bytes, store, sha256, data)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "changing the avatar of")
        profile = await _profile(conn, access, handle, lock=True)
        _deleted(profile, handle)
        if store is not None:
            try:
                await _hold_avatar(conn, store, access, user.user_id, sha256, data)
            except BlobStoreUnavailable:
                raise HTTPException(503, UNAVAILABLE) from None
        if profile.avatar_sha256 != sha256:
            version = await new_version(conn, profile, {"avatar_sha256": sha256}, user.user_id)
            target = f"{_target(profile, version)} fields=avatar_sha256"
            await Auditor.of(conn, user, access).record(audit.AGENT_UPDATE, target)
            profile = await _profile(conn, access, handle)
    return agent_view(profile)


async def avatar(request: Request, project: str, handle: str, user: Principal) -> tuple[bytes, str, str]:
    """(bytes, media type, SHA-256) of the profile's avatar; 404 when it has none."""
    async with request.app.state.engine.begin() as conn:
        profile = await _readable(conn, user, project, handle)
    if profile.avatar_sha256 is None:
        raise NotFound(f"@{handle} has no avatar: the web draws its initial on its colour")
    store = blob_store(request)
    try:
        data = await asyncio.to_thread(fetch_bytes, store, profile.avatar_sha256, AVATAR_MAX_BYTES)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    kind = media_type(data or b"")
    if data is None or kind is None:
        raise NotFound(f"the avatar of @{handle} is not in the blob store")
    return data, kind, profile.avatar_sha256
