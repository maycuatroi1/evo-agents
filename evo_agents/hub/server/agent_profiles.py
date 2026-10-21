"""The agent profiles of a project (``evo_agents.hub.profiles``): GET and POST /v1/projects/{p}/agents, GET, PATCH and
DELETE .../agents/{handle}, GET .../agents/{handle}/versions, and PUT, DELETE and GET .../agents/{handle}/avatar.

Every member of the project reads its profiles, soft-deleted ones included, since an old message keeps its author's
name and avatar; someone without a grant gets the 404 of ``project_access``, a hub admin without one a 403. Creating,
editing and deleting a profile, and its avatar, need the admin role on the project: a writer or a reader gets 403.

A handle is unique on the hub, deleted profiles' included, and may not be the name of a project (409): @<project> is
that project's default profile, which the registration of the project created and which can never be deleted; its
permissions stay all and its instructions empty (422), while its name, description, colour and avatar may change. A
handle never changes. A profile attaches agent packs of plugins its project enabled, each pinned at a version of its
plugin (``agent_packs.pinned``; the default profile attaches none). Every edit that changes something, the avatar and
the packs included, raises the version by one and writes the version row; PATCH with ``if_version`` other than the version now is 409, nothing changed. Deleting sets
``deleted_at``: the profile takes no new run, and reads as before; its accounts
(``evo_agents.hub.server.agent_accounts``) are deleted in the same transaction.

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
import binascii
import hashlib
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, profiles, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.agent_accounts import retire_profile_accounts
from evo_agents.hub.server.agent_packs import PinnedPack, Pins, pinned, pinned_view
from evo_agents.hub.server.blobs import blob_store, fetch_bytes, put_bytes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.profile_store import Profile, profile_of, profile_select, write_version
from evo_agents.hub.server.projects import PRINTABLE, ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

AVATAR_KIND = "agent-avatar"
AVATAR_MAX_BYTES = 512 * 1024
AVATAR_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP"}
UNAVAILABLE = "the blob store did not answer; the avatar was not changed, try again shortly"

Handle = Annotated[str, Path(pattern=profiles.HANDLE_PATTERN, description="the agent's handle, @ left out")]
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
AVATAR_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


# Models


class AgentAxis(BaseModel):
    """One axis of a profile's permissions: every value (all), or those of ``allow`` alone (allowlist)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal[profiles.MODES] = profiles.ALL
    allow: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=profiles.MAX_ALLOW, description="empty for mode all"
    )

    @model_validator(mode="after")
    def _no_list_for_all(self) -> AgentAxis:
        if self.mode == profiles.ALL and self.allow:
            raise ValueError("an axis of mode all takes no allow list")
        return self


class AgentPermissions(BaseModel):
    """What the runs of a profile may use, on eight axes; an axis left out allows all."""

    model_config = ConfigDict(extra="forbid")

    credential: AgentAxis = Field(
        default_factory=AgentAxis, description="secret names, and github-app for the App's token"
    )
    tool: AgentAxis = Field(default_factory=AgentAxis, description="read, edit, bash, web, agent, mcp:<server>")
    skill: AgentAxis = Field(default_factory=AgentAxis, description="skill names, global or the project's")
    worker: AgentAxis = Field(default_factory=AgentAxis, description="worker names, or label:<label>")
    project: AgentAxis = Field(default_factory=AgentAxis, description="the other projects whose runs it may take")
    model: AgentAxis = Field(default_factory=AgentAxis, description="model names, as a runtime names them")
    runtime: AgentAxis = Field(
        default_factory=AgentAxis, description="claude-code, opencode, codex, and plugins' runtimes <plugin>/<name>"
    )
    environment: AgentAxis = Field(
        default_factory=AgentAxis,
        description="where the agent runs, in order of preference: host, the worker's own machine, and plugins' "
        "environments <plugin>/<name>, each a container; all runs on the host",
    )

    @model_validator(mode="after")
    def _known_items(self) -> AgentPermissions:
        for axis in profiles.AXES:
            rule: AgentAxis = getattr(self, axis)
            rule.allow = list(dict.fromkeys(profiles.normalized(axis, item) for item in rule.allow))
        found = profiles.problems(self.stored())
        if found:
            raise ValueError("; ".join(found))
        return self

    def stored(self) -> dict:
        """The permissions as the hub stores them: ``{"mode": "all"}``, or the mode with its allow list."""
        return {
            axis: {"mode": rule.mode, "allow": rule.allow} if rule.mode == profiles.ALLOWLIST else {"mode": rule.mode}
            for axis, rule in ((axis, getattr(self, axis)) for axis in profiles.AXES)
        }


DisplayName = Annotated[str, Field(min_length=1, max_length=profiles.MAX_NAME_CHARS, pattern=PRINTABLE)]
Description = Annotated[str, Field(max_length=profiles.MAX_DESCRIPTION_CHARS)]
Instructions = Annotated[str, Field(max_length=profiles.MAX_INSTRUCTIONS_CHARS)]
Color = Annotated[str, Field(pattern=profiles.COLOR_PATTERN, description="#rrggbb, lowercase")]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(pattern=profiles.HANDLE_PATTERN, description="unique on the hub, no project's name; it stays")
    name: DisplayName
    description: Description = ""
    instructions: Instructions = Field("", description="added to the system prompt of the profile's runs")
    color: Color | None = Field(None, description="default: one picked by the handle")
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    packs: Pins = Field(
        default_factory=list, description="the agent packs it attaches, in order; see GET .../agent-packs"
    )


class AgentEdit(BaseModel):
    """What to change; a field left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    name: DisplayName | None = None
    description: Description | None = None
    instructions: Instructions | None = None
    color: Color | None = None
    permissions: AgentPermissions | None = Field(None, description="the eight axes whole; an axis left out allows all")
    packs: Pins | None = Field(None, description="the agent packs it attaches, whole, in order; [] for none")
    if_version: int | None = Field(None, ge=1, description="409 unless the profile is at this version now")


class AvatarUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(
        min_length=1,
        max_length=(AVATAR_MAX_BYTES + 2) // 3 * 4,
        description=f"the image, base64: PNG, JPEG or WebP, at most {AVATAR_MAX_BYTES} bytes",
    )

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("data is not base64") from None
        return value


class Agent(BaseModel):
    """An agent profile, with field names close to an A2A Agent Card's (name, description, iconUrl)."""

    handle: str
    project: str
    name: str
    description: str
    instructions: str
    color: str
    icon_url: str | None = Field(description="GET of the avatar on this hub; null: draw the initial on the colour")
    avatar_sha256: str | None
    is_default: bool = Field(description="the project's default profile, @<project>: every permission")
    permissions: AgentPermissions
    restricted: list[str] = Field(description="the axes that are allowlists, in the order of the eight")
    packs: list[PinnedPack] = Field(description="the agent packs it attaches, in order, each at the version it pins")
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = Field(description="when it was deleted: it takes no new run, and reads as before")


class AgentVersion(BaseModel):
    version: int
    body: dict = Field(description="the profile as this version had it: handle, name, permissions and the rest")
    changed_by: str
    changed_at: datetime


# Reading


def icon_url(profile: Profile) -> str | None:
    if profile.avatar_sha256 is None:
        return None
    return f"/v1/projects/{profile.project}/agents/{profile.handle}/avatar?v={profile.avatar_sha256[:16]}"


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
        is_default=profile.is_default,
        permissions=AgentPermissions.model_validate(profile.permissions),
        restricted=profiles.restricted_axes(profile.permissions),
        packs=pinned_view(profile.packs),
        version=profile.version,
        created_by=profile.created_by,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        deleted_at=profile.deleted_at,
    )


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the agents of project {access.name} needs a grant on it")


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"{doing} the agents of project {access.name} needs the admin role on it; you hold {held}"
        )


async def _profile(conn: AsyncConnection, access: ProjectAccess, handle: str, *, lock: bool = False) -> Profile:
    """The profile ``handle`` of the project; 404 when the project has none."""
    ap = tables.agent_profiles
    query = profile_select().where(ap.c.project_id == access.project_id, ap.c.handle == handle)
    row = (await conn.execute(query.with_for_update(of=ap) if lock else query)).one_or_none()
    if row is None:
        raise HTTPException(
            404,
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`",
        )
    return profile_of(row)


@router.get("/{project}/agents", response_model=list[Agent], responses=READ_REFUSALS)
async def list_agents(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    deleted: Annotated[bool, Query(description="include the deleted ones")] = False,
) -> list[Agent]:
    """The agent profiles of the project, the default first, then by handle."""
    ap = tables.agent_profiles
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = profile_select().where(ap.c.project_id == access.project_id)
        if not deleted:
            query = query.where(ap.c.deleted_at.is_(None))
        rows = await conn.execute(query.order_by(ap.c.is_default.desc(), ap.c.handle))
        return [agent_view(profile_of(row)) for row in rows]


@router.get("/{project}/agents/{handle}", response_model=Agent, responses=READ_REFUSALS)
async def show_agent(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Agent:
    """One agent profile of the project, deleted or not."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        return agent_view(await _profile(conn, access, handle))


@router.get("/{project}/agents/{handle}/versions", response_model=list[AgentVersion], responses=READ_REFUSALS)
async def agent_versions(
    request: Request, project: ProjectName, handle: Handle, user: CurrentUser
) -> list[AgentVersion]:
    """Every version of the profile, the latest first: the rules a run of that version had."""
    v, u = tables.agent_profile_versions, tables.users
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        found = await _profile(conn, access, handle)
        query = (
            select(v.c.version, v.c.body, u.c.login.label("changed_by"), v.c.changed_at)
            .join_from(v, u, u.c.id == v.c.changed_by)
            .where(v.c.profile_id == found.id)
            .order_by(v.c.version.desc())
        )
        return [AgentVersion(**row._mapping) for row in await conn.execute(query)]


# Changing


def _target(profile: Profile, version: int | None = None) -> str:
    return f"{profile.project}/@{profile.handle} v{profile.version if version is None else version}"


async def _record(conn: AsyncConnection, user, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=target,
        project_id=access.project_id,
    )


async def _refuse_project_name(conn: AsyncConnection, handle: str) -> None:
    p = tables.projects
    if (await conn.execute(select(p.c.id).where(p.c.name == handle))).first() is not None:
        raise HTTPException(
            409,
            f"@{handle} is the name of project {handle}, whose default agent it names: pick another handle; nothing "
            "was created",
        )


@router.post("/{project}/agents", status_code=201, response_model=Agent, responses=WRITE_REFUSALS)
async def create_agent(request: Request, project: ProjectName, body: AgentCreate, user: CurrentUser) -> Agent:
    """Create an agent profile of the project, at version 1."""
    ap = tables.agent_profiles
    values = {
        "handle": body.handle,
        "name": body.name,
        "description": body.description,
        "instructions": body.instructions,
        "color": body.color or profiles.default_color(body.handle),
        "avatar_sha256": None,
        "is_default": False,
        "permissions": body.permissions.stored(),
    }
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "creating")
        await _refuse_project_name(conn, body.handle)
        values["packs"] = await pinned(conn, access.project_id, project, body.packs, request.app.state.hub_version)
        statement = (
            pg_insert(ap)
            .values(project_id=access.project_id, created_by=user.user_id, **values)
            .on_conflict_do_nothing(index_elements=[ap.c.handle])
            .returning(ap.c.id)
        )
        profile_id = (await conn.execute(statement)).scalar_one_or_none()
        if profile_id is None:
            raise HTTPException(
                409, f"@{body.handle} is taken by another agent, deleted ones included: pick another handle"
            )
        await write_version(conn, profile_id, 1, values, user.user_id)
        created = await _profile(conn, access, body.handle)
        await _record(conn, user, access, audit.AGENT_CREATE, _target(created))
    log.info("agent created", extra={"project": project, "handle": body.handle, "login": user.login})
    return agent_view(created)


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
            raise HTTPException(
                422,
                f"@{profile.handle} is the default agent of project {profile.project}: its permissions stay all, so a "
                "run that names no agent runs as before; create another agent to limit them",
            )
        if wanted.get("instructions") or wanted.get("packs"):
            raise HTTPException(
                422,
                f"@{profile.handle} is the default agent of project {profile.project}: it has no instructions and "
                "attaches no agent pack, so a run that names no agent runs as before; create another agent to give "
                "it some",
            )
    current = profile.snapshot()
    return {key: value for key, value in wanted.items() if value is not None and current[key] != value}


async def new_version(conn: AsyncConnection, profile: Profile, changes: dict, user_id: int) -> int:
    """Write ``changes`` to ``profile`` as its next version, with its version row; the new version."""
    ap = tables.agent_profiles
    version = profile.version + 1
    await conn.execute(
        update(ap).values(**changes, version=version, updated_at=func.now()).where(ap.c.id == profile.id)
    )
    await write_version(conn, profile.id, version, {**profile.snapshot(), **changes}, user_id)
    return version


@router.patch("/{project}/agents/{handle}", response_model=Agent, responses=WRITE_REFUSALS)
async def edit_agent(
    request: Request, project: ProjectName, handle: Handle, body: AgentEdit, user: CurrentUser
) -> Agent:
    """Change an agent profile: the fields given, as its next version; nothing changes for values it has already."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "editing")
        profile = await _profile(conn, access, handle, lock=True)
        if profile.deleted_at is not None:
            raise HTTPException(409, f"@{handle} was deleted at {profile.deleted_at.isoformat()}: it changes no more")
        if body.if_version is not None and body.if_version != profile.version:
            raise HTTPException(
                409, f"@{handle} is at version {profile.version}, not {body.if_version}: read it again; nothing changed"
            )
        packs = await pinned(conn, access.project_id, project, body.packs, request.app.state.hub_version)
        changes = profile_changes(profile, body, packs)
        if changes:
            version = await new_version(conn, profile, changes, user.user_id)
            fields = ",".join(sorted(changes))
            await _record(conn, user, access, audit.AGENT_UPDATE, f"{_target(profile, version)} fields={fields}")
            profile = await _profile(conn, access, handle)
    log.info("agent edited", extra={"project": project, "handle": handle, "changed": sorted(changes)})
    return agent_view(profile)


@router.delete("/{project}/agents/{handle}", response_model=Agent, responses=WRITE_REFUSALS)
async def delete_agent(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Agent:
    """Soft-delete an agent profile: it takes no new run; its handle stays taken and it reads as before."""
    ap = tables.agent_profiles
    retired = 0
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "deleting")
        profile = await _profile(conn, access, handle, lock=True)
        if profile.is_default:
            raise HTTPException(409, f"@{handle} is the default agent of project {project}, which is never deleted")
        if profile.deleted_at is None:
            await conn.execute(update(ap).values(deleted_at=func.now()).where(ap.c.id == profile.id))
            retired = await retire_profile_accounts(conn, profile.id)
            await _record(conn, user, access, audit.AGENT_DELETE, _target(profile))
            profile = await _profile(conn, access, handle)
    log.info("agent deleted", extra={"project": project, "handle": handle, "login": user.login})
    if retired:
        log.info("agent accounts deleted with it", extra={"project": project, "handle": handle, "accounts": retired})
    return agent_view(profile)


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


def _image(body: AvatarUpload) -> tuple[bytes, str]:
    data = base64.b64decode(body.data, validate=True)
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(413, f"the image is {len(data)} bytes, over the {AVATAR_MAX_BYTES} an avatar may have")
    if media_type(data) is None:
        raise HTTPException(422, f"an avatar is a {', '.join(AVATAR_TYPES.values())} image, and this one is none")
    return data, hashlib.sha256(data).hexdigest()


async def _hold_avatar(conn: AsyncConnection, store, access: ProjectAccess, user_id: int, sha256: str, data: bytes):
    """Record that the project holds blob ``sha256``, put back first if a deletion was pending for it."""
    await blob_gc.lock_shared(conn)
    if await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(put_bytes, store, sha256, data, replace=True)
    b = tables.blobs
    await conn.execute(
        pg_insert(b)
        .values(project_id=access.project_id, sha256=sha256, size=len(data), kind=AVATAR_KIND, created_by=user_id)
        .on_conflict_do_nothing()
    )


async def _set_avatar(request: Request, project: str, handle: str, user, sha256: str | None, data: bytes = b""):
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
        if profile.deleted_at is not None:
            raise HTTPException(409, f"@{handle} was deleted at {profile.deleted_at.isoformat()}: it changes no more")
        if store is not None:
            try:
                await _hold_avatar(conn, store, access, user.user_id, sha256, data)
            except BlobStoreUnavailable:
                raise HTTPException(503, UNAVAILABLE) from None
        if profile.avatar_sha256 != sha256:
            version = await new_version(conn, profile, {"avatar_sha256": sha256}, user.user_id)
            await _record(conn, user, access, audit.AGENT_UPDATE, f"{_target(profile, version)} fields=avatar_sha256")
            profile = await _profile(conn, access, handle)
    return agent_view(profile)


@router.put("/{project}/agents/{handle}/avatar", response_model=Agent, responses=AVATAR_REFUSALS)
async def put_avatar(
    request: Request, project: ProjectName, handle: Handle, body: AvatarUpload, user: CurrentUser
) -> Agent:
    """Set the profile's avatar, a PNG, JPEG or WebP image, as its next version."""
    async with request.app.state.engine.begin() as conn:  # the refusals of the caller before the bytes are stored
        _admin(await project_access(conn, user, project), "changing the avatar of")
    data, sha256 = _image(body)
    view = await _set_avatar(request, project, handle, user, sha256, data)
    log.info("agent avatar set", extra={"project": project, "handle": handle, "bytes": len(data)})
    return view


@router.delete("/{project}/agents/{handle}/avatar", response_model=Agent, responses=WRITE_REFUSALS)
async def delete_avatar(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Agent:
    """Drop the profile's avatar, as its next version: the web draws its initial on its colour again."""
    return await _set_avatar(request, project, handle, user, None)


@router.get(
    "/{project}/agents/{handle}/avatar",
    response_class=Response,
    responses={200: {"content": {kind: {} for kind in AVATAR_TYPES}}, **READ_REFUSALS, 503: {"model": ErrorBody}},
)
async def get_avatar(request: Request, project: ProjectName, handle: Handle, user: CurrentUser) -> Response:
    """The profile's avatar image; 404 when it has none."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        profile = await _profile(conn, access, handle)
    if profile.avatar_sha256 is None:
        raise HTTPException(404, f"@{handle} has no avatar: the web draws its initial on its colour")
    store = blob_store(request)
    try:
        data = await asyncio.to_thread(fetch_bytes, store, profile.avatar_sha256, AVATAR_MAX_BYTES)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    kind = media_type(data or b"")
    if kind is None:
        raise HTTPException(404, f"the avatar of @{handle} is not in the blob store")
    headers = {
        "Cache-Control": "private, max-age=86400",
        "ETag": f'"{profile.avatar_sha256}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
    }
    return Response(content=data, media_type=kind, headers=headers)
