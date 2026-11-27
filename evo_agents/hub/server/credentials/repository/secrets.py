"""The queries of the owner's secrets (``service.secrets``, and the accounts, provider accounts and answers that write
secrets too): their rows and bindings, the count a member keeps, the upsert of a value, the soft delete."""

from __future__ import annotations

from sqlalchemy import Text, column, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server.account_oauth import drop_tokens


def bound_names(target, key):
    """The names of the rows of ``target`` (projects or workers) the secret's bindings name by ``key``, each once and
    sorted, as an array; correlated to the secret of the enclosing query."""
    b = tables.secret_bindings
    names = (
        select(target.c.name)
        .distinct()
        .join_from(b, target, target.c.id == key)
        .where(b.c.secret_id == tables.secrets.c.id)
        .order_by(target.c.name)
    )
    return func.array(names.scalar_subquery(), type_=ARRAY(Text))


async def owned_secret_rows(conn: AsyncConnection, owner_id: int, name: str | None = None) -> list:
    """The rows of the live secrets of ``owner_id`` that are not a provider account's key, by name; ``name`` alone
    when given. Each with the names of its projects, workers and pools."""
    s, b = tables.secrets, tables.secret_bindings
    listed = select(
        s.c.id,
        s.c.name,
        s.c.kind,
        s.c.env_var,
        s.c.url_prefix,
        s.c.username,
        s.c.environment,
        bound_names(tables.projects, b.c.project_id).label("projects"),
        bound_names(tables.workers, b.c.worker_id).label("workers"),
        bound_names(tables.worker_pools, b.c.pool_id).label("pools"),
        s.c.expires_at,
        s.c.created_at,
        s.c.updated_at,
    ).where(s.c.owner_id == owner_id, s.c.kind != PROVIDER_KIND, s.c.deleted_at.is_(None))
    if name is not None:
        listed = listed.where(s.c.name == name)
    return list((await conn.execute(listed.order_by(s.c.name))).all())


async def worker_ids(conn: AsyncConnection, owner_id: int, names: list[str]) -> dict[str, int]:
    """Lowercased name -> id of the workers ``names`` of ``owner_id`` that are not revoked."""
    w = tables.workers
    lower = func.lower(w.c.name)
    own = select(lower.label("name"), w.c.id).where(
        w.c.owner_id == owner_id, w.c.revoked_at.is_(None), lower.in_([name.lower() for name in names])
    )
    return {row.name: row.id for row in await conn.execute(own)}


async def pool_ids(conn: AsyncConnection, owner_id: int, names: list[str]) -> dict[str, int]:
    """Lowercased name -> id of the worker pools ``names`` of ``owner_id``."""
    p = tables.worker_pools
    lower = func.lower(p.c.name)
    own = select(lower.label("name"), p.c.id).where(
        p.c.owner_id == owner_id, lower.in_([name.lower() for name in names])
    )
    return {row.name: row.id for row in await conn.execute(own)}


async def lock_owner(conn: AsyncConnection, owner_id: int) -> None:
    """Lock the row of member ``owner_id``: one write of a member's secrets at a time, so two of them cannot both pass
    the count."""
    users = tables.users
    await conn.execute(select(users.c.id).where(users.c.id == owner_id).with_for_update())


async def live_secrets(conn: AsyncConnection, owner_id: int, other_than: str) -> int:
    s = tables.secrets
    live = (
        select(func.count())
        .select_from(s)
        .where(s.c.owner_id == owner_id, s.c.deleted_at.is_(None), s.c.name != other_than)
    )
    return (await conn.execute(live)).scalar_one()


async def provider_holds(conn: AsyncConnection, owner_id: int, name: str) -> bool:
    """Whether the live secret ``name`` of ``owner_id`` is a provider account's key, which /v1/secrets never writes."""
    s = tables.secrets
    held = select(s.c.id).where(
        s.c.owner_id == owner_id, s.c.name == name, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None)
    )
    return (await conn.execute(held)).first() is not None


def upsert_secret(owner_id: int, name: str, **values):
    """Insert the live secret ``name`` of ``owner_id``, or replace the one there is, whole; RETURNING its id and
    whether it was inserted: xmax is 0 on a row this statement inserted, and the updating transaction's id on a row it
    replaced."""
    s = tables.secrets
    statement = pg_insert(s).values(owner_id=owner_id, name=name, **values)
    replaced = {key: statement.excluded[key] for key in values} | {"updated_at": func.now()}
    return statement.on_conflict_do_update(
        index_elements=[s.c.owner_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
    ).returning(s.c.id, (column("xmax") == 0).label("created"))


async def bind_secret(
    conn: AsyncConnection,
    secret_id: int,
    project_ids: list[int],
    worker_ids: list[int],
    pool_ids: list[int] = (),  # pyright: ignore[reportArgumentType]
) -> None:
    """The secret's bindings, replaced: each project on each of the workers, or on each of the pools, or on any worker
    of its owner."""
    b = tables.secret_bindings
    await conn.execute(delete(b).where(b.c.secret_id == secret_id))
    targets = [{"worker_id": worker_id} for worker_id in worker_ids] or [{"pool_id": pool_id} for pool_id in pool_ids]
    rows = [
        {"secret_id": secret_id, "project_id": project_id, "worker_id": None, "pool_id": None, **target}
        for project_id in project_ids
        for target in targets or [{}]
    ]
    if rows:
        await conn.execute(insert(b), rows)


async def upsert(conn: AsyncConnection, owner_id: int, name: str, **values) -> tuple[int, bool]:
    """(id, created) of the live secret ``name`` of ``owner_id``, inserted or replaced whole (``upsert_secret``)."""
    secret_id, created = (await conn.execute(upsert_secret(owner_id, name, **values))).one()
    return secret_id, created


async def bound_projects(conn: AsyncConnection, secret_id: int) -> list[int]:
    """The ids of the projects the secret's bindings name, each once."""
    b = tables.secret_bindings
    bound = select(b.c.project_id).distinct().where(b.c.secret_id == secret_id).order_by(b.c.project_id)
    return list((await conn.execute(bound)).scalars())


async def live_secret(conn: AsyncConnection, owner_id: int, name: str):
    """(id, kind) of the live secret ``name`` of ``owner_id``, its row locked; None for none."""
    s = tables.secrets
    found = (
        select(s.c.id, s.c.kind)
        .where(s.c.owner_id == owner_id, s.c.name == name, s.c.deleted_at.is_(None))
        .with_for_update()
    )
    return (await conn.execute(found)).one_or_none()


async def deletable_secret(conn: AsyncConnection, owner_id: int, name: str) -> int | None:
    """The id of the live secret ``name`` of ``owner_id`` that is not a provider account's key, locked; None for none."""
    s = tables.secrets
    found = (
        select(s.c.id)
        .where(s.c.owner_id == owner_id, s.c.name == name, s.c.kind != PROVIDER_KIND, s.c.deleted_at.is_(None))
        .with_for_update()
    )
    return (await conn.execute(found)).scalar_one_or_none()


async def retire_secret(conn: AsyncConnection, secret_id: int, actor_id: int) -> int:
    """Delete the secret ``secret_id`` softly: its sealed value, tokens and bindings go, its leases and shares are
    revoked by ``actor_id``; the number of leases revoked."""
    s, b, cl, cs = tables.secrets, tables.secret_bindings, tables.credential_leases, tables.credential_shares
    gone = update(s).values(deleted_at=func.now(), sealed=None, nonce=None, key_id=None)
    await conn.execute(gone.where(s.c.id == secret_id))
    await conn.execute(delete(b).where(b.c.secret_id == secret_id))
    await drop_tokens(conn, secret_id)
    ended = await conn.execute(
        update(cl).values(revoked_at=func.now()).where(cl.c.secret_id == secret_id, cl.c.revoked_at.is_(None))
    )
    unshared = update(cs).values(revoked_at=func.now(), revoked_by=actor_id)
    await conn.execute(unshared.where(cs.c.secret_id == secret_id, cs.c.revoked_at.is_(None)))
    return ended.rowcount
