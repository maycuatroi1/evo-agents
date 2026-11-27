"""The SQL of hub administration (``evo_agents.hub.server.admin``): users with their live tokens and grants, a
registered project, giving and taking a grant, the row counts of every table, and the counts of the overview."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import (
    BigInteger,
    and_,
    any_,
    cast,
    column,
    delete,
    func,
    literal,
    or_,
    select,
    table,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by, array_agg
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.runs import OFFLINE_AFTER_SECONDS
from evo_agents.hub.server.hub_admin.models.admin import (
    ACTIVE_DAYS,
    AUDIT_HOURS,
    FAILED_BUILD_DAYS,
    TOKEN_EXPIRING_DAYS,
    TOKEN_UNUSED_DAYS,
)
from evo_agents.hub.server.paging import Paging

# The conditions on a row of tokens that the overview counts and GET /v1/admin/tokens?state= lists, so a count and the
# list it links to agree. A token expires TOKEN_TTL after its last use, so with TOKEN_TTL at 90 days an unused token is
# also an expired one that nobody revoked.
_tokens = tables.tokens
TOKEN_LIVE = and_(_tokens.c.revoked_at.is_(None), _tokens.c.expires_at > func.now())
TOKEN_EXPIRING = and_(TOKEN_LIVE, _tokens.c.expires_at <= func.now() + timedelta(days=TOKEN_EXPIRING_DAYS))
TOKEN_UNUSED = and_(
    _tokens.c.revoked_at.is_(None),
    func.coalesce(_tokens.c.last_used_at, _tokens.c.created_at) <= func.now() - timedelta(days=TOKEN_UNUSED_DAYS),
)


def _users(admin_login):
    """Every user by login whatever its case, with the number of their live tokens; ``admin_login`` is the column of
    ``security.admin_login``."""
    users, tokens = tables.users, tables.tokens
    live = select(func.count()).select_from(tokens).where(tokens.c.user_id == users.c.id, TOKEN_LIVE)
    return select(
        users.c.id,
        users.c.login,
        admin_login.label("admin_login"),
        users.c.github_id.is_not(None).label("signed_in"),
        users.c.created_at,
        users.c.last_seen_at,
        live.scalar_subquery().label("active_tokens"),
    ).order_by(func.lower(users.c.login))


def _user_grants(user_ids: list[int]):
    """The grants of the users ``user_ids`` by project, with the login of the admin who gave each."""
    grants, projects, users = tables.grants, tables.projects, tables.users
    return (
        select(
            grants.c.user_id,
            projects.c.name.label("project"),
            grants.c.role,
            grants.c.max_level,
            users.c.login.label("granted_by"),
            grants.c.granted_at,
        )
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .outerjoin(users, users.c.id == grants.c.granted_by)
        .where(grants.c.user_id == any_(literal(user_ids, ARRAY(BigInteger))))
        .order_by(projects.c.name)
    )


def upsert_grant(user_id: int, project_id: int, role: str, max_level: str, granted_by: int):
    """Give the grant, or change the one held; RETURNING whether the row is new (``xmax`` is 0 only for an insert)."""
    grants = tables.grants
    given = pg_insert(grants).values(
        user_id=user_id, project_id=project_id, role=role, max_level=max_level, granted_by=granted_by
    )
    return given.on_conflict_do_update(
        index_elements=[grants.c.user_id, grants.c.project_id],
        set_={
            "role": given.excluded.role,
            "max_level": given.excluded.max_level,
            "granted_by": given.excluded.granted_by,
            "granted_at": func.now(),
        },
    ).returning((column("xmax") == 0).label("created"))


def _table_names():
    """The tables of the hub's schema by name, but Alembic's record of the revision."""
    pg_tables = table("pg_tables", column("schemaname"), column("tablename"))
    return (
        select(pg_tables.c.tablename)
        .where(pg_tables.c.schemaname == func.current_schema(), pg_tables.c.tablename != "alembic_version")
        .order_by(pg_tables.c.tablename)
    )


def _row_counts(names: list[str]):
    """One row per table of ``names``: its name and its number of rows."""
    return union_all(*(select(literal(name), func.count()).select_from(table(name)) for name in names))


def _counts():
    """One row of every count. Each table is read once; blobs are counted by object, as several projects may hold one.
    Each count is a CTE of one row, and the CTEs are joined on true."""
    users, tokens, blobs = tables.users, tables.tokens, tables.blobs
    blob_deletions, workers, audit_trail = tables.blob_deletions, tables.workers, tables.audit
    members = select(
        func.count().label("total"),
        func.count().filter(users.c.last_seen_at > func.now() - timedelta(days=ACTIVE_DAYS)).label("active"),
        func.count().filter(users.c.github_id.is_(None)).label("not_signed_in"),
    ).cte("members")
    credentials = (
        select(
            func.count().filter(TOKEN_LIVE).label("live"),
            func.count().filter(TOKEN_EXPIRING).label("expiring"),
            func.count().filter(TOKEN_UNUSED).label("unused"),
        )
        .where(tokens.c.revoked_at.is_(None))
        .cte("credentials")
    )
    objects = select(func.max(blobs.c.size).label("size")).group_by(blobs.c.sha256).subquery("objects")
    stored = select(
        func.count().label("objects"),
        cast(func.coalesce(func.sum(objects.c.size), 0), BigInteger).label("bytes"),
    ).cte("stored")
    doomed = (
        select(
            func.count().label("objects"),
            cast(func.coalesce(func.sum(blob_deletions.c.size), 0), BigInteger).label("bytes"),
        )
        .where(blob_deletions.c.deleted_at.is_(None))
        .cte("doomed")
    )
    silent = or_(
        workers.c.last_heartbeat_at.is_(None),
        workers.c.last_heartbeat_at < func.now() - timedelta(seconds=OFFLINE_AFTER_SECONDS),
    )
    fleet = (
        select(func.count().label("live"), func.count().filter(silent).label("offline"))
        .where(workers.c.revoked_at.is_(None))
        .cte("fleet")
    )
    trail = (
        select(func.count().label("rows"))
        .where(audit_trail.c.at > func.now() - timedelta(hours=AUDIT_HOURS))
        .cte("trail")
    )
    return select(
        members.c.total.label("members"),
        members.c.active.label("active_members"),
        members.c.not_signed_in,
        credentials.c.live.label("live_tokens"),
        credentials.c.expiring.label("expiring_tokens"),
        credentials.c.unused.label("unused_tokens"),
        stored.c.objects,
        stored.c.bytes,
        doomed.c.objects.label("pending_deletions"),
        doomed.c.bytes.label("pending_bytes"),
        fleet.c.live.label("live_workers"),
        fleet.c.offline.label("offline_workers"),
        trail.c.rows.label("audit_rows"),
    ).select_from(
        members.join(credentials, true())
        .join(stored, true())
        .join(doomed, true())
        .join(fleet, true())
        .join(trail, true())
    )


def _grant_counts():
    """Every project by name, with its grants by role."""
    projects, grants = tables.projects, tables.grants
    return (
        select(
            projects.c.name.label("project"),
            func.count().filter(grants.c.role == "admin").label("admins"),
            func.count().filter(grants.c.role == "writer").label("writers"),
            func.count().filter(grants.c.role == "reader").label("readers"),
        )
        .select_from(projects.outerjoin(grants, grants.c.project_id == projects.c.id))
        .group_by(projects.c.id, projects.c.name)
        .order_by(projects.c.name)
    )


def _failed_builds():
    """The projects with a build that failed within FAILED_BUILD_DAYS, latest failure first, each with its newest
    build, which comes from kg_builds_project_idx: one probe per project with a failure."""
    projects, kg_builds = tables.projects, tables.kg_builds
    # PostgreSQL's own aggregate_order_by: indexing Core's form renders array_agg(...)[1], which Postgres refuses
    # without parentheses around the call.
    last_id = array_agg(aggregate_order_by(kg_builds.c.id, kg_builds.c.finished_at.desc(), kg_builds.c.id.desc()))
    failed = (
        select(
            kg_builds.c.project_id,
            func.count().label("failed"),
            last_id[1].label("last_id"),
            func.max(kg_builds.c.finished_at).label("last_at"),
        )
        .where(
            kg_builds.c.status == "failed",
            kg_builds.c.finished_at > func.now() - timedelta(days=FAILED_BUILD_DAYS),
        )
        .group_by(kg_builds.c.project_id)
        .subquery("f")
    )
    newest = kg_builds.alias("b")
    latest = (
        select(newest.c.id, newest.c.status)
        .where(newest.c.project_id == failed.c.project_id)
        .order_by(newest.c.id.desc())
        .limit(1)
        .lateral("latest")
    )
    return (
        select(
            projects.c.name.label("project"),
            failed.c.failed,
            failed.c.last_id.label("last_failed_id"),
            failed.c.last_at.label("last_failed_at"),
            latest.c.id.label("latest_id"),
            latest.c.status.label("latest_status"),
        )
        .select_from(failed.join(projects, projects.c.id == failed.c.project_id).join(latest, true()))
        .order_by(failed.c.last_at.desc(), projects.c.name)
    )


async def users_page(conn: AsyncConnection, page: Paging, admin_login):
    """The page ``page`` of every user, and the grants of its users; ``admin_login`` is ``security.admin_login()``."""
    rows = await page.rows(conn, _users(admin_login))
    grant_rows = (await conn.execute(_user_grants([row.id for row in rows]))).all()
    return rows, grant_rows


async def project_row(conn: AsyncConnection, name: str):
    """(id, levels, locations) of the registered project ``name``, or None."""
    projects = tables.projects
    found = await conn.execute(
        select(projects.c.id, projects.c.levels, projects.c.locations).where(projects.c.name == name)
    )
    return found.first()


async def add_login(conn: AsyncConnection, login: str) -> int | None:
    """The id of the users row made for ``login``; None when it has one already."""
    users = tables.users
    made = await conn.execute(pg_insert(users).values(login=login).on_conflict_do_nothing().returning(users.c.id))
    return made.scalar()


async def user_of_login(conn: AsyncConnection, login: str) -> tuple[int, str]:
    """(id, login) of the user ``login`` names, whatever its case."""
    users = tables.users
    found = await conn.execute(select(users.c.id, users.c.login).where(func.lower(users.c.login) == func.lower(login)))
    grantee_id, grantee = found.one()
    return grantee_id, grantee


async def put_grant(conn: AsyncConnection, user_id: int, project_id: int, role: str, max_level: str, by: int) -> bool:
    """Give the grant, or change the one held; whether it is new."""
    upserted = await conn.execute(upsert_grant(user_id, project_id, role, max_level, by))
    return upserted.scalar_one()


async def delete_grant(conn: AsyncConnection, project_id: int, login: str):
    """Delete the grant ``login`` holds on project ``project_id``; (login, id) of its user, or None for no grant."""
    grants, users = tables.grants, tables.users
    deleted = await conn.execute(
        delete(grants)
        .where(
            grants.c.user_id == users.c.id,
            grants.c.project_id == project_id,
            func.lower(users.c.login) == func.lower(login),
        )
        .returning(users.c.login, users.c.id)
    )
    return deleted.first()


async def row_counts(conn: AsyncConnection):
    """(name, rows) of every table of the hub's schema."""
    names = (await conn.execute(_table_names())).scalars().all()
    return (await conn.execute(_row_counts(names))).all() if names else []  # pyright: ignore[reportArgumentType]


async def overview_rows(conn: AsyncConnection):
    """The counts of the overview, the grants of every project, and the projects with a failed build."""
    count = (await conn.execute(_counts())).one()._mapping
    grants = (await conn.execute(_grant_counts())).all()
    failed = (await conn.execute(_failed_builds())).all()
    return count, grants, failed
