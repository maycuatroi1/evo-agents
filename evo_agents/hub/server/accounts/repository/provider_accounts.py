"""The SQL of provider accounts, a member's and an agent's (``evo_agents.hub.server.accounts.service.provider_accounts``
says what they are): the rows of the accounts, the sealed key of an env secret a write names, the name a write takes,
and the writes of the ``secrets``, ``provider_accounts`` and ``accounts`` rows."""

from __future__ import annotations

from sqlalchemy import column, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server.accounts.repository.accounts import bound_names

# Reading


def provider_rows(where, name: str | None = None):
    """The live provider accounts that ``where`` (a clause on secrets) keeps, with their endpoint, bindings and
    settings, by name; ``name`` alone, when given."""
    s, p, a, b = tables.secrets, tables.provider_accounts, tables.accounts, tables.secret_bindings
    statement = (
        select(
            s.c.id,
            s.c.name,
            p.c.provider,
            p.c.base_url,
            p.c.api_format,
            p.c.models,
            bound_names(tables.projects, b.c.project_id).label("projects"),
            bound_names(tables.workers, b.c.worker_id).label("workers"),
            func.coalesce(a.c.weight, 1).label("weight"),
            func.coalesce(a.c.priority, 0).label("priority"),
            a.c.paused_at,
            a.c.cooldown_until,
            a.c.cooldown_reason,
            a.c.last_leased_at,
            s.c.expires_at,
            s.c.created_at,
            s.c.updated_at,
            func.now().label("now"),
        )
        .join_from(s, p, p.c.secret_id == s.c.id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(where, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None))
        .order_by(s.c.name)
    )
    return statement if name is None else statement.where(s.c.name == name)


async def owned(conn: AsyncConnection, owner_id: int, name: str | None = None) -> list:
    """The rows of ``owner_id``'s live provider accounts (``name`` alone, when given)."""
    return list((await conn.execute(provider_rows(tables.secrets.c.owner_id == owner_id, name))).all())


async def own(conn: AsyncConnection, owner_id: int, name: str, *, lock: bool = False):
    """The row of ``owner_id``'s live account ``name`` with its sealed key, locked with ``lock``; None when none."""
    s = tables.secrets
    statement = provider_rows(s.c.owner_id == owner_id, name).add_columns(s.c.sealed, s.c.nonce, s.c.key_id)
    return (await conn.execute(statement.with_for_update(of=s) if lock else statement)).one_or_none()


async def env_secret(conn: AsyncConnection, owner_id: int, name: str):
    """(sealed, nonce, key_id) of ``owner_id``'s live env secret ``name``; None when there is none."""
    s = tables.secrets
    found = select(s.c.sealed, s.c.nonce, s.c.key_id).where(
        s.c.owner_id == owner_id, s.c.name == name, s.c.kind == "env", s.c.deleted_at.is_(None)
    )
    return (await conn.execute(found)).one_or_none()


async def lock_owner(conn: AsyncConnection, owner_id: int) -> None:
    """Lock the row of user ``owner_id`` until the transaction ends, so two writes of theirs go one after the other."""
    users = tables.users
    await conn.execute(select(users.c.id).where(users.c.id == owner_id).with_for_update())


async def kind_named(conn: AsyncConnection, owner_id: int, name: str) -> str | None:
    """The kind of ``owner_id``'s live secret ``name``; None when there is none."""
    s = tables.secrets
    held = select(s.c.kind).where(s.c.owner_id == owner_id, s.c.name == name, s.c.deleted_at.is_(None))
    return (await conn.execute(held)).scalar_one_or_none()


# Writing


async def upsert(conn: AsyncConnection, owner_id: int, name: str, **values) -> tuple[int, bool]:
    """``owner_id``'s provider account ``name`` made, or its live row replaced, with ``values``: (its id, whether this
    write made it)."""
    s = tables.secrets
    statement = pg_insert(s).values(owner_id=owner_id, name=name, kind=PROVIDER_KIND, **values)
    replaced = {key: statement.excluded[key] for key in values} | {"updated_at": func.now()}
    written = statement.on_conflict_do_update(
        index_elements=[s.c.owner_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
    ).returning(s.c.id, (column("xmax") == 0).label("created"))
    secret_id, created = (await conn.execute(written)).one()
    return secret_id, created


async def update_secret(conn: AsyncConnection, secret_id: int, values: dict) -> None:
    """Secret ``secret_id``'s row changed by ``values``, and its update time."""
    s = tables.secrets
    await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == secret_id))


async def store_endpoint(conn: AsyncConnection, secret_id: int, **values) -> None:
    """The account's ``provider_accounts`` row, made or changed: provider, base_url, api_format and models given."""
    p = tables.provider_accounts
    made = pg_insert(p).values(secret_id=secret_id, **values)
    await conn.execute(
        made.on_conflict_do_update(index_elements=[p.c.secret_id], set_={**values, "updated_at": func.now()})
    )


async def store_settings(conn: AsyncConnection, secret_id: int, weight=None, priority=None, paused=None) -> None:
    """Weight, priority and pause of the account, in its ``accounts`` row (made when there is none)."""
    a = tables.accounts
    values = {key: given for key, given in (("weight", weight), ("priority", priority)) if given is not None}
    if paused is not None:
        values["paused_at"] = func.now() if paused else None
    if not values:
        return
    kept = {**values}
    if paused:
        kept["paused_at"] = func.coalesce(a.c.paused_at, func.now())
    made = pg_insert(a).values(secret_id=secret_id, **values)
    await conn.execute(
        made.on_conflict_do_update(index_elements=[a.c.secret_id], set_={**kept, "updated_at": func.now()})
    )
