"""The registry of a project's signal keys and its ingest keys (``evo_agents.hub.signals``).

GET /v1/projects/{project}/signal-keys lists the keys for a member with a grant, the archived ones too with
``archived``. An admin of the project adds a key (POST, 201; 409 when the name is taken, archived or not) with its kind,
``number`` or ``feedback``, its unit, description, source and the level its signals get when they name none, a level of
the project's ladder; and archives one (POST .../{name}/archive), after which it takes no new signal and its signals
stay readable.

GET /v1/projects/{project}/signal-ingest-keys lists the ingest keys for an admin of the project, the revoked ones too
with ``revoked``. An admin creates one for a service (POST, 201) with a name unique among the project's live keys and
the highest level it writes (the project's default level when left out); the answer holds the token, ``evi_...``, the
one time the hub shows it: the hub keeps only its SHA-256. Revoking one (POST .../{id}/revoke) takes effect on the next
batch sent with it. Each change writes an audit row naming the project and the key, never a token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_signals import (
    INGEST_NAME,
    KEY_NAME,
    MAX_DESCRIPTION_CHARS,
    MAX_KEY_NAME_CHARS,
    MAX_LEVEL_CHARS,
    MAX_SOURCE_CHARS,
    MAX_UNIT_CHARS,
)

PRINTABLE = r"^[^\x00-\x1f\x7f]*$"
UNIQUE_VIOLATION = "23505"
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
KeyName = Annotated[
    str, Path(max_length=MAX_KEY_NAME_CHARS, pattern=KEY_NAME, description="the key, as kb.search.hits")
]
IngestId = Annotated[int, Path(ge=1, le=2**63 - 1)]

router = APIRouter(prefix="/v1/projects", tags=["signals"], responses={401: {"model": ErrorBody}})


class SignalKeyIn(BaseModel):
    name: str = Field(max_length=MAX_KEY_NAME_CHARS, pattern=KEY_NAME, description="two to six dotted parts")
    kind: Literal["number", "feedback"]
    unit: str = Field(default="", max_length=MAX_UNIT_CHARS, pattern=PRINTABLE)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    source: str = Field(default="", max_length=MAX_SOURCE_CHARS, pattern=PRINTABLE)
    level: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_LEVEL_CHARS,
        description="the level its signals get when they name none; the project's default level when left out",
    )


class SignalKey(BaseModel):
    name: str
    kind: Literal["number", "feedback"]
    unit: str
    description: str
    source: str
    level: str | None
    created_by: str
    created_at: datetime
    archived_at: datetime | None


class SignalIngestKeyIn(BaseModel):
    name: str = Field(max_length=100, pattern=INGEST_NAME, description="the service it is for, unique among live keys")
    max_level: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_LEVEL_CHARS,
        description="the highest level it writes; the project's default level when left out",
    )


class SignalIngestKey(BaseModel):
    id: int
    name: str
    max_level: str
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None


class SignalIngestKeyIssued(SignalIngestKey):
    token: str = Field(description="evi_...: shown this once, the hub keeps only its SHA-256")


# Who may do what


async def _access(conn: AsyncConnection, user: Principal, project: str, role: str, what: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    if not has_role(access.role, role):
        held = access.role or "no grant"
        raise HTTPException(403, f"{what} of project {project} needs the {role} role on it; you hold {held}")
    return access


def _level(access: ProjectAccess, level: str | None, what: str) -> str | None:
    if level is not None and level not in access.rules.levels:
        levels = ", ".join(access.rules.levels)
        raise HTTPException(422, f"{what} {level!r} is not a level of project {access.name} ({levels})")
    return level


async def _taken(conn: AsyncConnection, statement):
    """``statement``'s row, or 409 when a write at the same moment took its unique name."""
    try:
        async with conn.begin_nested():
            return (await conn.execute(statement)).one()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != UNIQUE_VIOLATION:
            raise
        raise HTTPException(
            409, "another write took this name at the same moment: list the keys and try again"
        ) from None


# The registry of keys


def _keys_query(project_id: int):
    sk, users = tables.signal_keys, tables.users
    return (
        select(
            sk.c.name,
            sk.c.kind,
            sk.c.unit,
            sk.c.description,
            sk.c.source,
            sk.c.level,
            users.c.login.label("created_by"),
            sk.c.created_at,
            sk.c.archived_at,
        )
        .select_from(sk.join(users, users.c.id == sk.c.created_by))
        .where(sk.c.project_id == project_id)
        .order_by(sk.c.name)
    )


@router.get("/{project}/signal-keys", response_model=list[SignalKey], responses=REFUSALS)
async def list_keys(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    archived: Annotated[bool, Query(description="archived keys too")] = False,
) -> list[SignalKey]:
    """The keys of the project's registry, by name."""
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project, "reader", "reading the signal keys")
        query = _keys_query(access.project_id)
        if not archived:
            query = query.where(tables.signal_keys.c.archived_at.is_(None))
        return [SignalKey(**row._mapping) for row in await conn.execute(query)]


@router.post("/{project}/signal-keys", status_code=201, response_model=SignalKey, responses=REFUSALS)
async def add_key(request: Request, project: ProjectName, body: SignalKeyIn, user: CurrentUser) -> SignalKey:
    """Add a key to the project's registry: from now on the hub takes signals of it."""
    sk = tables.signal_keys
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project, "admin", "adding a signal key")
        level = _level(access, body.level, "level")
        held = (
            await conn.execute(
                select(sk.c.archived_at).where(sk.c.project_id == access.project_id, sk.c.name == body.name)
            )
        ).one_or_none()
        if held is not None:
            archived = ", archived" if held.archived_at else ""
            raise HTTPException(409, f"project {project} has a signal key {body.name} already{archived}")
        values = body.model_dump(exclude={"level"}) | {"level": level}
        statement = insert(sk).values(project_id=access.project_id, created_by=user.user_id, **values)
        await _taken(conn, statement.returning(sk.c.id))
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.SIGNAL_KEY_ADD,
            target=f"{project} key={body.name}",
            project_id=access.project_id,
        )
        row = (await conn.execute(_keys_query(access.project_id).where(sk.c.name == body.name))).one()
    return SignalKey(**row._mapping)


@router.post("/{project}/signal-keys/{name}/archive", response_model=SignalKey, responses=REFUSALS)
async def archive_key(request: Request, project: ProjectName, name: KeyName, user: CurrentUser) -> SignalKey:
    """Archive a key: it takes no new signal, and its signals stay. Archiving it again changes nothing."""
    sk = tables.signal_keys
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project, "admin", "archiving a signal key")
        found = await conn.execute(
            update(sk)
            .where(sk.c.project_id == access.project_id, sk.c.name == name, sk.c.archived_at.is_(None))
            .values(archived_at=func.now(), archived_by=user.user_id)
            .returning(sk.c.id)
        )
        if found.one_or_none() is not None:
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.SIGNAL_KEY_ARCHIVE,
                target=f"{project} key={name}",
                project_id=access.project_id,
            )
        row = (await conn.execute(_keys_query(access.project_id).where(sk.c.name == name))).one_or_none()
    if row is None:
        raise HTTPException(404, f"project {project} has no signal key {name}: see `evo-agents hub signal keys list`")
    return SignalKey(**row._mapping)


# Ingest keys


def _ingest_query(project_id: int):
    ik, users = tables.signal_ingest_keys, tables.users
    revoker = users.alias("revoker")
    return (
        select(
            ik.c.id,
            ik.c.name,
            ik.c.max_level,
            users.c.login.label("created_by"),
            ik.c.created_at,
            ik.c.last_used_at,
            ik.c.revoked_at,
            revoker.c.login.label("revoked_by"),
        )
        .select_from(ik.join(users, users.c.id == ik.c.created_by).outerjoin(revoker, revoker.c.id == ik.c.revoked_by))
        .where(ik.c.project_id == project_id)
        .order_by(ik.c.id)
    )


@router.get("/{project}/signal-ingest-keys", response_model=list[SignalIngestKey], responses=REFUSALS)
async def list_ingest_keys(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    revoked: Annotated[bool, Query(description="revoked keys too")] = False,
) -> list[SignalIngestKey]:
    """The project's ingest keys, oldest first; never a token."""
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project, "admin", "reading the ingest keys")
        query = _ingest_query(access.project_id)
        if not revoked:
            query = query.where(tables.signal_ingest_keys.c.revoked_at.is_(None))
        return [SignalIngestKey(**row._mapping) for row in await conn.execute(query)]


@router.post("/{project}/signal-ingest-keys", status_code=201, response_model=SignalIngestKeyIssued, responses=REFUSALS)
async def create_ingest_key(
    request: Request, project: ProjectName, body: SignalIngestKeyIn, user: CurrentUser
) -> SignalIngestKeyIssued:
    """A new ingest key for a service; its token is in this answer and nowhere else."""
    ik = tables.signal_ingest_keys
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project, "admin", "creating an ingest key")
        max_level = _level(access, body.max_level, "max_level") or access.rules.default_label.get("level")
        live = select(ik.c.id).where(
            ik.c.project_id == access.project_id, ik.c.name == body.name, ik.c.revoked_at.is_(None)
        )
        if (await conn.execute(live)).first() is not None:
            raise HTTPException(409, f"project {project} has a live ingest key {body.name}: revoke it or pick a name")
        token = signals.new_ingest_token()
        values = {"project_id": access.project_id, "name": body.name, "max_level": max_level}
        statement = insert(ik).values(**values, token_hash=signals.hash_token(token), created_by=user.user_id)
        key_id = (await _taken(conn, statement.returning(ik.c.id)))[0]
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.SIGNAL_INGEST_CREATE,
            target=f"{project} ingest:{key_id} name={body.name}",
            project_id=access.project_id,
        )
        row = (await conn.execute(_ingest_query(access.project_id).where(ik.c.id == key_id))).one()
    return SignalIngestKeyIssued(**row._mapping, token=token)


@router.post("/{project}/signal-ingest-keys/{key_id}/revoke", response_model=SignalIngestKey, responses=REFUSALS)
async def revoke_ingest_key(
    request: Request, project: ProjectName, key_id: IngestId, user: CurrentUser
) -> SignalIngestKey:
    """Revoke an ingest key: the next batch sent with it gets 401. Revoking it again changes nothing."""
    ik = tables.signal_ingest_keys
    async with request.app.state.engine.begin() as conn:
        access = await _access(conn, user, project, "admin", "revoking an ingest key")
        revoked = await conn.execute(
            update(ik)
            .where(ik.c.project_id == access.project_id, ik.c.id == key_id, ik.c.revoked_at.is_(None))
            .values(revoked_at=func.now(), revoked_by=user.user_id)
            .returning(ik.c.name)
        )
        name = revoked.scalar_one_or_none()
        if name is not None:
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.SIGNAL_INGEST_REVOKE,
                target=f"{project} ingest:{key_id} name={name}",
                project_id=access.project_id,
            )
        row = (await conn.execute(_ingest_query(access.project_id).where(ik.c.id == key_id))).one_or_none()
    if row is None:
        raise HTTPException(
            404, f"project {project} has no ingest key {key_id}: see `evo-agents hub signal ingest list`"
        )
    return SignalIngestKey(**row._mapping)
