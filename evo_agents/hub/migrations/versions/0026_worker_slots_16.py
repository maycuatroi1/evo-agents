"""A worker holds up to 16 runs at once, and its owner changes its slots after it joined.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-13

Schema 0009 bounded workers.slots and worker_pairings.slots to 1 through 8, and workers.free_slots, what the last
heartbeat reported, to 0 through 8. This revision widens the three CHECKs to 16 (``MAX_SLOTS``), the bound the api
holds a pairing, a registration, PATCH /v1/workers/{id} and a heartbeat to. The constraints keep the names Postgres
gave them in 0009. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables`` describes the tables.

Going back narrows the CHECKs to 8 again, which is the schema that schema 0025 runs on. It refuses, naming them, while
a live worker (not revoked) or a live pairing (neither used nor expired) holds more than 8 slots: its owner sets them
to 8 or fewer first (PATCH /v1/workers/{id}), or the pairing expires. A revoked worker and a used or expired pairing
only record what was, so going back sets their slots to 8, and every worker's free_slots to at most 8, as a heartbeat
of schema 0025 would report them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

MAX_SLOTS = 16  # evo_agents.hub.runs.MAX_SLOTS as of this revision, a copy rather than an import
OLD_MAX_SLOTS = 8

col = sa.column
BOUNDS = (("workers", "slots", 1), ("workers", "free_slots", 0), ("worker_pairings", "slots", 1))


def _bound(high: int) -> None:
    """Replace the CHECK of each bounded column with one from its low to ``high``, under the name 0009 gave it."""
    for table, column, low in BOUNDS:
        name = f"{table}_{column}_check"
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, col(column).between(low, high))


def upgrade() -> None:
    _bound(MAX_SLOTS)


def downgrade() -> None:
    workers = sa.table("workers", col("id"), col("slots"), col("free_slots"), col("revoked_at"))
    pairings = sa.table("worker_pairings", col("id"), col("slots"), col("used_at"), col("expires_at"))
    conn = op.get_bind()
    live_workers = (
        sa.select(workers.c.id)
        .where(workers.c.revoked_at.is_(None), workers.c.slots > OLD_MAX_SLOTS)
        .order_by(workers.c.id)
    )
    live_pairings = (
        sa.select(pairings.c.id)
        .where(pairings.c.used_at.is_(None), pairings.c.expires_at > sa.func.now(), pairings.c.slots > OLD_MAX_SLOTS)
        .order_by(pairings.c.id)
    )
    held = [f"worker {i}" for i in conn.execute(live_workers).scalars()]
    held += [f"pairing {i}" for i in conn.execute(live_pairings).scalars()]
    if held:
        raise RuntimeError(
            f"schema 0025 holds a worker to {OLD_MAX_SLOTS} slots, and {', '.join(held)} "
            f"{'has' if len(held) == 1 else 'have'} more: set each worker to {OLD_MAX_SLOTS} slots or fewer "
            "(PATCH /v1/workers/{id}) and let each pairing expire, then go back again"
        )
    op.execute(workers.update().where(workers.c.slots > OLD_MAX_SLOTS).values(slots=OLD_MAX_SLOTS))  # revoked only
    op.execute(pairings.update().where(pairings.c.slots > OLD_MAX_SLOTS).values(slots=OLD_MAX_SLOTS))  # used, expired
    op.execute(workers.update().where(workers.c.free_slots > OLD_MAX_SLOTS).values(free_slots=OLD_MAX_SLOTS))
    _bound(OLD_MAX_SLOTS)
