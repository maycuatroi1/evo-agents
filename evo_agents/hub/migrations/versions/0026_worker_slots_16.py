"""A worker holds up to 16 runs at once, and its owner changes its slots after it joined.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-13

Schema 0009 bounded workers.slots and worker_pairings.slots to 1 through 8, and workers.free_slots, what the last
heartbeat reported, to 0 through 8. This revision widens the three CHECKs to 16 (``MAX_SLOTS``), the bound the api
holds a pairing, a registration, PATCH /v1/workers/{id} and a heartbeat to. The constraints keep the names Postgres
gave them in 0009. The statements are Alembic operations on SQL text, as 0009 wrote the constraints.

Going back narrows the CHECKs to 8 again, which is the schema that schema 0025 runs on. It refuses, naming them, while
a live worker (not revoked) or a live pairing (neither used nor expired) holds more than 8 slots: its owner sets them
to 8 or fewer first (PATCH /v1/workers/{id}), or the pairing expires. A revoked worker and a used or expired pairing
only record what was, so going back sets their slots to 8, and every worker's free_slots to at most 8, as a heartbeat
of schema 0025 would report them.
"""

from alembic import op
from sqlalchemy import text

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

MAX_SLOTS = 16
OLD_MAX_SLOTS = 8


def _bound(table: str, column: str, low: int, high: int) -> str:
    name = f"{table}_{column}_check"
    return (
        f"ALTER TABLE {table} DROP CONSTRAINT {name}, ADD CONSTRAINT {name} CHECK ({column} BETWEEN {low} AND {high})"
    )


def _bounds(high: int) -> list[str]:
    return [
        _bound("workers", "slots", 1, high),
        _bound("workers", "free_slots", 0, high),
        _bound("worker_pairings", "slots", 1, high),
    ]


def upgrade() -> None:
    for statement in _bounds(MAX_SLOTS):
        op.execute(statement)


LIVE_WORKERS = text("SELECT id FROM workers WHERE revoked_at IS NULL AND slots > :most ORDER BY id")
LIVE_PAIRINGS = text(
    "SELECT id FROM worker_pairings WHERE used_at IS NULL AND expires_at > now() AND slots > :most ORDER BY id"
)


def downgrade() -> None:
    conn = op.get_bind()
    workers = [row[0] for row in conn.execute(LIVE_WORKERS, {"most": OLD_MAX_SLOTS})]
    pairings = [row[0] for row in conn.execute(LIVE_PAIRINGS, {"most": OLD_MAX_SLOTS})]
    if workers or pairings:
        held = [f"worker {i}" for i in workers] + [f"pairing {i}" for i in pairings]
        raise RuntimeError(
            f"schema 0025 holds a worker to {OLD_MAX_SLOTS} slots, and {', '.join(held)} "
            f"{'has' if len(held) == 1 else 'have'} more: set each worker to {OLD_MAX_SLOTS} slots or fewer "
            "(PATCH /v1/workers/{id}) and let each pairing expire, then go back again"
        )
    op.execute(f"UPDATE workers SET slots = {OLD_MAX_SLOTS} WHERE slots > {OLD_MAX_SLOTS}")  # revoked ones only
    op.execute(f"UPDATE worker_pairings SET slots = {OLD_MAX_SLOTS} WHERE slots > {OLD_MAX_SLOTS}")  # used, expired
    op.execute(f"UPDATE workers SET free_slots = {OLD_MAX_SLOTS} WHERE free_slots > {OLD_MAX_SLOTS}")
    for statement in _bounds(OLD_MAX_SLOTS):
        op.execute(statement)
