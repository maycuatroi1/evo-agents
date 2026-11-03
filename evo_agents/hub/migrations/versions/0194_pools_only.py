"""Every agent runs on its own pool: the placement bridge and the legacy worker keys go.

Revision ID: 0194
Revises: 0193
Create Date: 2026-11-03

agent_profiles.placement (0189) is dropped with its check: every agent goes to a node of its own pool now. The worker
or pool a charter names goes too: charters and schedules lose worker_id and pool_id, with their keys and the check that
held exactly one of them, and the night shift runs on the pool of the project's default agent. project_agent_types
loses ``workers`` (and its check keeps the name and the daily cap alone). The keys ``worker``, ``pool`` and ``labels``
of ``loop`` and ``worker`` of ``judge`` leave the stored policies (autonomy_policies.body), and ``worker`` and ``pool``
leave the stored charters (charters.body), so the hub's models, which refuse a key they do not know, read every old
revision.

The values still named are carried over first: a worker that a charter, a schedule, a policy's ``loop.worker`` or
``judge.worker`` or an agent type's ``workers`` names, which belongs to no pool, is live and serves the project, and
belongs to the member who owns the pool of the project's default agent, joins that pool. Nothing else moves: a worker
in a pool stays there, and a pool named by a charter, a loop or an agent stays as it is.

Going back restores the columns and keys empty (``workers`` of the agent types stays empty, their values are not
kept), sets every agent to ``pool``, and points the charters and schedules at the first live node of the pool of the
project's default agent, or at the pool when it has no node (going back below 0189 deletes such a row, as 0150's own
downgrade deletes the rows of a pool).
The check that holds one of worker or pool is added not valid, so a row the previous hub cannot place does not stop
the downgrade. The policy and charter keys removed above are not restored.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0194"
down_revision = "0193"
branch_labels = None
depends_on = None

PROFILES, TYPES = "agent_profiles", "project_agent_types"
NIGHT = ("charters", "schedules")
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"  # as 0042
MAX_DAILY_USD = 1000
MAX_WORKERS = 16
col = sa.column

workers = sa.table(
    "workers",
    col("id"),
    col("name"),
    col("owner_id"),
    col("pool_id"),
    col("revoked_at"),
)
pools = sa.table("worker_pools", col("id"), col("owner_id"), col("agent_id"))
profiles = sa.table(PROFILES, col("id"), col("project_id"), col("is_default"), col("deleted_at"), col("placement"))
serves = sa.table("worker_projects", col("worker_id"), col("project_id"))
policies = sa.table("autonomy_policies", col("project_id"), col("revision"), sa.column("body", postgresql.JSONB))
charters = sa.table(
    "charters",
    col("project_id"),
    col("revision"),
    sa.column("body", postgresql.JSONB),
    col("worker_id"),
    col("pool_id"),
)
schedules = sa.table("schedules", col("project_id"), col("worker_id"), col("pool_id"))
types = sa.table(TYPES, col("project_id"), col("run_as"), col("workers"))
NIGHT_TABLES = {"charters": charters, "schedules": schedules}
STRIP = {"loop": ("worker", "pool", "labels"), "judge": ("worker",)}  # the keys of autonomy_policies.body that go
CHARTER_KEYS = ("worker", "pool")


def _default_pools(bind) -> dict[int, tuple[int, int]]:
    """project id -> (pool id, the member who owns it): the pool of the project's default agent."""
    query = (
        sa.select(profiles.c.project_id, pools.c.id, pools.c.owner_id)
        .select_from(pools.join(profiles, profiles.c.id == pools.c.agent_id))
        .where(profiles.c.is_default, profiles.c.deleted_at.is_(None))
    )
    return {row.project_id: (row.id, row.owner_id) for row in bind.execute(query)}


def _named(bind) -> set[tuple[int, int]]:
    """(worker id, project id) of each worker a charter, a schedule, a policy's loop or judge, or an agent type names."""
    named: set[tuple[int, int]] = set()
    for table in (charters, schedules):
        query = sa.select(table.c.worker_id, table.c.project_id).where(table.c.worker_id.is_not(None))
        named.update((row.worker_id, row.project_id) for row in bind.execute(query))
    newest: dict[int, dict] = {}
    for row in bind.execute(sa.select(policies.c.project_id, policies.c.body).order_by(policies.c.revision)):
        newest[row.project_id] = row.body  # the last revision of each project wins
    for project_id, body in newest.items():
        for key in ("loop", "judge"):
            name = (body.get(key) or {}).get("worker")
            if name:
                query = sa.select(workers.c.id).where(sa.func.lower(workers.c.name) == str(name).lower())
                named.update((row.id, project_id) for row in bind.execute(query))
    for row in bind.execute(sa.select(types.c.project_id, types.c.run_as, types.c.workers)):
        wanted = [name.lower() for name in row.workers or ()]
        if wanted:
            query = sa.select(workers.c.id).where(
                workers.c.owner_id == row.run_as, sa.func.lower(workers.c.name).in_(wanted)
            )
            named.update((found.id, row.project_id) for found in bind.execute(query))
    return named


def _carry(bind) -> None:
    home = _default_pools(bind)
    for worker_id, project_id in sorted(_named(bind)):
        if project_id not in home:
            continue
        pool_id, owner_id = home[project_id]
        serving = sa.exists().where(serves.c.worker_id == workers.c.id, serves.c.project_id == project_id)
        bind.execute(
            sa.update(workers)
            .where(
                workers.c.id == worker_id,
                workers.c.pool_id.is_(None),
                workers.c.revoked_at.is_(None),
                workers.c.owner_id == owner_id,
                serving,
            )
            .values(pool_id=pool_id)
        )


def _strip(bind) -> None:
    for row in bind.execute(sa.select(policies.c.project_id, policies.c.revision, policies.c.body)).all():
        body = {**row.body}
        for key, gone in STRIP.items():
            if isinstance(body.get(key), dict) and any(name in body[key] for name in gone):
                body[key] = {k: v for k, v in body[key].items() if k not in gone}
        if body != row.body:
            bind.execute(
                sa.update(policies)
                .where(policies.c.project_id == row.project_id, policies.c.revision == row.revision)
                .values(body=body)
            )
    for row in bind.execute(sa.select(charters.c.project_id, charters.c.revision, charters.c.body)).all():
        if any(name in row.body for name in CHARTER_KEYS):
            kept = {k: v for k, v in row.body.items() if k not in CHARTER_KEYS}
            bind.execute(
                sa.update(charters)
                .where(charters.c.project_id == row.project_id, charters.c.revision == row.revision)
                .values(body=kept)
            )


def _point_back(bind, table) -> None:
    """The first live node of the default agent's pool, or the pool when it has no node, for each row of ``table``."""
    for project_id, (pool_id, _) in _default_pools(bind).items():
        node = bind.execute(
            sa.select(sa.func.min(workers.c.id)).where(workers.c.pool_id == pool_id, workers.c.revoked_at.is_(None))
        ).scalar()
        values = {"worker_id": node} if node is not None else {"pool_id": pool_id}
        bind.execute(sa.update(table).where(table.c.project_id == project_id).values(**values))


def _types_check(*, with_workers: bool) -> None:
    rules = [col("name").regexp_match(NAME_PATTERN), col("daily_usd") > 0, col("daily_usd") <= MAX_DAILY_USD]
    if with_workers:
        rules.append(sa.func.cardinality(col("workers")) <= MAX_WORKERS)
    op.drop_constraint("project_agent_types_check", TYPES, type_="check")
    op.create_check_constraint("project_agent_types_check", TYPES, sa.and_(*rules))


def upgrade() -> None:
    _carry(op.get_bind())
    op.drop_constraint(f"{PROFILES}_placement_check", PROFILES, type_="check")
    op.drop_column(PROFILES, "placement")
    for table in NIGHT:
        op.drop_constraint(f"{table}_worker_or_pool_check", table, type_="check")
        op.drop_constraint(f"{table}_pool_id_fkey", table, type_="foreignkey")
        op.drop_constraint(f"{table}_worker_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "pool_id")
        op.drop_column(table, "worker_id")
    _strip(op.get_bind())
    _types_check(with_workers=False)
    op.drop_column(TYPES, "workers")


def downgrade() -> None:
    op.add_column(TYPES, sa.Column("workers", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False))
    _types_check(with_workers=True)
    for table in NIGHT:
        op.add_column(table, sa.Column("worker_id", sa.BigInteger(), nullable=True))
        op.add_column(table, sa.Column("pool_id", sa.BigInteger(), nullable=True))
        op.create_foreign_key(f"{table}_worker_id_fkey", table, "workers", ["worker_id"], ["id"], ondelete="RESTRICT")
        op.create_foreign_key(f"{table}_pool_id_fkey", table, "worker_pools", ["pool_id"], ["id"], ondelete="RESTRICT")
        _point_back(op.get_bind(), NIGHT_TABLES[table])
        op.create_check_constraint(
            f"{table}_worker_or_pool_check",
            table,
            col("worker_id").is_(None) != col("pool_id").is_(None),
            postgresql_not_valid=True,
        )
    op.add_column(PROFILES, sa.Column("placement", sa.Text(), server_default="legacy", nullable=False))
    op.get_bind().execute(sa.update(profiles).values(placement="pool"))
    op.create_check_constraint(f"{PROFILES}_placement_check", PROFILES, col("placement").in_(["legacy", "pool"]))
