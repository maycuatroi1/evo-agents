"""The SQL of the accounts of an agent: the live env accounts of profiles, the profile an admin changes, the workers
an account binds to, its settings, and the soft delete of accounts (``retire_accounts``)."""

from __future__ import annotations

from sqlalchemy import column, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND


def account_rows(names_of, profile_ids: list[int], name: str | None = None):
    """The live accounts of the profiles ``profile_ids`` (``name`` alone, when given), by variable and name; their
    workers are named by ``names_of`` (``secrets.bound_names``, which the service passes in: ``secrets`` reaches the
    service)."""
    s, a, b = tables.secrets, tables.accounts, tables.secret_bindings
    statement = (
        select(
            s.c.id,
            s.c.profile_id,
            s.c.name,
            s.c.env_var,
            names_of(tables.workers, b.c.worker_id).label("workers"),
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
        .outerjoin_from(s, a, a.c.secret_id == s.c.id)
        .where(s.c.profile_id.in_(profile_ids), s.c.kind == "env", s.c.deleted_at.is_(None))
        .order_by(s.c.env_var, s.c.name)
    )
    return statement if name is None else statement.where(s.c.name == name)


async def accounts_of(conn: AsyncConnection, names_of, profile_ids: list[int], name: str | None = None) -> list:
    """The rows of ``account_rows``."""
    return list((await conn.execute(account_rows(names_of, profile_ids, name))).all())


async def account_of(conn: AsyncConnection, names_of, profile_id: int, name: str):
    """The row of ``account_rows`` of the live account ``name`` of the profile, which exists."""
    return (await conn.execute(account_rows(names_of, [profile_id], name))).one()


async def locked_profile(conn: AsyncConnection, project_id: int, handle: str):
    """(id, is_default, deleted_at) of profile ``handle`` of the project, its row locked; None when it has none."""
    ap = tables.agent_profiles
    query = (
        select(ap.c.id, ap.c.is_default, ap.c.deleted_at)
        .where(ap.c.project_id == project_id, ap.c.handle == handle)
        .with_for_update(of=ap)
    )
    return (await conn.execute(query)).one_or_none()


async def usable_workers(conn: AsyncConnection, user_id: int, project_id: int, names: list[str]) -> list:
    """(lower-case name, id, owner_id) of each worker of ``names`` that is not revoked, the member's own or shared,
    and serves the project."""
    w, wp = tables.workers, tables.worker_projects
    lower = func.lower(w.c.name)
    usable = select(lower.label("name"), w.c.id, w.c.owner_id).where(
        w.c.revoked_at.is_(None),
        lower.in_([name.lower() for name in names]),
        or_(w.c.owner_id == user_id, w.c.shared.is_(True)),
        select(wp.c.worker_id).where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id).exists(),
    )
    return list(await conn.execute(usable))


async def bind_agent_account(conn: AsyncConnection, secret_id: int, project_id: int, worker_ids: list[int]) -> None:
    """The account's bindings, replaced: the profile's project on each of the workers."""
    b = tables.secret_bindings
    await conn.execute(delete(b).where(b.c.secret_id == secret_id))
    await conn.execute(
        insert(b), [{"secret_id": secret_id, "project_id": project_id, "worker_id": worker} for worker in worker_ids]
    )


async def account_settings(
    conn: AsyncConnection, secret_id: int, weight: int | None, priority: int | None, paused=None
):
    """Weight, priority and pause of the account, written to its ``accounts`` row (made when there is none)."""
    a = tables.accounts
    values = {key: given for key, given in (("weight", weight), ("priority", priority)) if given is not None}
    paused_at = {} if paused is None else {"paused_at": func.now() if paused else None}
    if not values and not paused_at:
        return
    made = pg_insert(a).values(secret_id=secret_id, **values, **paused_at)
    kept = {"paused_at": func.coalesce(a.c.paused_at, func.now()) if paused else None} if paused_at else {}
    changed = {**values, **kept, "updated_at": func.now()}
    await conn.execute(made.on_conflict_do_update(index_elements=[a.c.secret_id], set_=changed))


async def retire_accounts(conn: AsyncConnection, secret_ids: list[int]) -> int:
    """Delete the accounts ``secret_ids`` in the transaction of ``conn``: the sealed values and the bindings go, the
    rows are marked deleted, and the leases of them not given back are marked revoked. The number of leases revoked."""
    if not secret_ids:
        return 0
    s, b, cl, a = tables.secrets, tables.secret_bindings, tables.credential_leases, tables.accounts
    await conn.execute(
        update(s).values(deleted_at=func.now(), sealed=None, nonce=None, key_id=None).where(s.c.id.in_(secret_ids))
    )
    await conn.execute(delete(b).where(b.c.secret_id.in_(secret_ids)))
    await conn.execute(
        update(a).values(oauth_sealed=None, oauth_nonce=None, oauth_key_id=None).where(a.c.secret_id.in_(secret_ids))
    )
    ended = await conn.execute(
        update(cl).values(revoked_at=func.now()).where(cl.c.secret_id.in_(secret_ids), cl.c.revoked_at.is_(None))
    )
    return ended.rowcount


async def retire_profile_accounts(conn: AsyncConnection, profile_id: int) -> int:
    """Delete every live account of the profile, as deleting the profile does; how many there were."""
    s = tables.secrets
    ids = (await conn.execute(select(s.c.id).where(s.c.profile_id == profile_id, s.c.deleted_at.is_(None)))).scalars()
    ids = list(ids)
    await retire_accounts(conn, ids)
    return len(ids)


async def agent_provider_holds(conn: AsyncConnection, profile_id: int, name: str) -> bool:
    """Whether the live account ``name`` of the profile is a provider account, which these routes never write."""
    s = tables.secrets
    held = select(s.c.id).where(
        s.c.profile_id == profile_id, s.c.name == name, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None)
    )
    return (await conn.execute(held)).first() is not None


async def live_agent_accounts(conn: AsyncConnection, profile_id: int, other_than: str) -> int:
    s = tables.secrets
    live = (
        select(func.count())
        .select_from(s)
        .where(s.c.profile_id == profile_id, s.c.deleted_at.is_(None), s.c.name != other_than)
    )
    return (await conn.execute(live)).scalar_one()


async def held_kind(conn: AsyncConnection, profile_id: int, name: str) -> str | None:
    """The kind of the live account ``name`` of the profile; None when it has none."""
    s = tables.secrets
    held = select(s.c.kind).where(s.c.profile_id == profile_id, s.c.name == name, s.c.deleted_at.is_(None))
    return (await conn.execute(held)).scalar_one_or_none()


async def put_secret(conn: AsyncConnection, profile_id: int, name: str, kind: str, fields: dict) -> tuple[int, bool]:
    """(id, whether it was made) of the live secret ``name`` of kind ``kind`` of the profile, made or replaced with
    ``fields``."""
    s = tables.secrets
    statement = pg_insert(s).values(profile_id=profile_id, name=name, kind=kind, **fields)
    replaced = {key: statement.excluded[key] for key in fields} | {"updated_at": func.now()}
    statement = statement.on_conflict_do_update(
        index_elements=[s.c.profile_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
    ).returning(s.c.id, (column("xmax") == 0).label("created"))
    secret_id, created = (await conn.execute(statement)).one()
    return secret_id, created


async def locked_env_account(conn: AsyncConnection, profile_id: int, name: str) -> int | None:
    """The id of the live env account ``name`` of the profile, its row locked; None when it has none."""
    s = tables.secrets
    found = select(s.c.id).where(
        s.c.profile_id == profile_id, s.c.name == name, s.c.kind == "env", s.c.deleted_at.is_(None)
    )
    return (await conn.execute(found.with_for_update())).scalar_one_or_none()


async def change_secret(conn: AsyncConnection, secret_id: int, values: dict) -> None:
    """Write ``values`` to secret ``secret_id``, as written now."""
    s = tables.secrets
    await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == secret_id))


async def profile_id_of(conn: AsyncConnection, project_id: int, handle: str) -> int | None:
    """The id of profile ``handle`` of the project, deleted or not; None when it has none."""
    ap = tables.agent_profiles
    found = select(ap.c.id).where(ap.c.project_id == project_id, ap.c.handle == handle)
    return (await conn.execute(found)).scalar_one_or_none()


async def listed_agents(conn: AsyncConnection, user_id: int, project: str | None, handle: str | None) -> list:
    """(id, handle, name, color, avatar_sha256, project) of the live, non-default agents of every project the user
    holds a grant on (``project`` and ``handle`` narrow it), by project and handle."""
    ap, projects, grants = tables.agent_profiles, tables.projects, tables.grants
    agents = (
        select(ap.c.id, ap.c.handle, ap.c.name, ap.c.color, ap.c.avatar_sha256, projects.c.name.label("project"))
        .join_from(ap, projects, projects.c.id == ap.c.project_id)
        .join(grants, (grants.c.project_id == projects.c.id) & (grants.c.user_id == user_id))
        .where(ap.c.deleted_at.is_(None), ap.c.is_default.is_(False))
        .order_by(projects.c.name, ap.c.handle)
    )
    if project is not None:
        agents = agents.where(projects.c.name == project)
    if handle is not None:
        agents = agents.where(ap.c.handle == handle)
    return list((await conn.execute(agents)).all())
