"""The hub runtime: a chat run of an agent that runs on the hub has runtime 'hub' and no worker.

Revision ID: 0219
Revises: 0218
Create Date: 2026-11-07

An agent whose profile says runs_on hub answers through the hub's gateway (plan multi-agent-systems, step 4,
``evo_agents.hub.server.hub_runtime``): a job of the hub's queue takes each turn, so its run holds no worker. The two
runtime CHECKs take 'hub' besides the names of 0039 (RUNTIME_NAME), and runs_check5, which held every run past queued
to a worker, takes a run of runtime 'hub' without one. The bounds are copies of ``evo_agents.hub.runs`` as of this
revision, never imports.

Going back deletes the runs of runtime 'hub' that no other run names as the run it retries, reruns or goes on from,
until none is left, then puts the CHECKs of 0039 and 0033 back; a run of the hub runtime another run still names
stops it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0219"
down_revision = "0218"
branch_labels = None
depends_on = None

HUB = "hub"  # evo_agents.hub.runs.HUB_RUNTIME
RUNTIME_NAME = "^(any|claude-code|opencode|codex|[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9_-]{0,62})$"  # as of 0039
COLUMNS = ("requested_runtime", "runtime")
UNCLAIMED = ("queued", "cancelled", "failed")

col = sa.column


def _runtime_checks(rule) -> None:
    for name in COLUMNS:
        op.drop_constraint(f"runs_{name}_check", "runs", type_="check")
        op.create_check_constraint(f"runs_{name}_check", "runs", rule(col(name)))


def _claimed(holder) -> None:
    """runs_check5: a run past queued is a local run, or one a holder took with a runtime and a lease time."""
    op.drop_constraint("runs_check5", "runs", type_="check")
    op.create_check_constraint(
        "runs_check5",
        "runs",
        sa.or_(
            col("state").in_(UNCLAIMED),
            col("kind") == "local",
            sa.and_(holder, col("runtime") != "any", col("leased_at").is_not(None)),
        ),
    )


def upgrade() -> None:
    _runtime_checks(lambda column: sa.or_(column.regexp_match(RUNTIME_NAME), column == HUB))
    _claimed(sa.or_(col("worker_id").is_not(None), col("runtime") == HUB))


def downgrade() -> None:
    runs = sa.table("runs", col("id"), col("requested_runtime"), col("parent_run_id"), col("resume_of_run_id"))
    child = runs.alias("child")
    of_hub = runs.c.requested_runtime == HUB
    named = sa.exists().where(sa.or_(child.c.parent_run_id == runs.c.id, child.c.resume_of_run_id == runs.c.id))
    conn = op.get_bind()
    while conn.execute(runs.delete().where(of_hub, ~named)).rowcount:
        pass
    left = conn.execute(sa.select(runs.c.id).where(of_hub).order_by(runs.c.id)).scalars().all()
    if left:
        raise RuntimeError(f"runs {left} of the hub runtime are named by runs of another runtime; delete them first")
    _claimed(col("worker_id").is_not(None))
    _runtime_checks(lambda column: column.regexp_match(RUNTIME_NAME))
