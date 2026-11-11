"""The ops threads of a project's default system: the rule ``ops`` of orchestrator_threads, the thread of a batch, and
the breaker of a system's ops.

Revision ID: 0241
Revises: 0240
Create Date: 2026-11-12

The names below are copies of ``evo_agents.hub.tables_orchestrator``, ``tables_ops`` and ``tables_systems`` as of this
revision, never imports, so a later change to them takes a new revision.

orchestrator_threads takes the rule ``ops`` (plan hub-steward, criterion a8): the thread the hub opens to the
Orchestrator of a project's default system for a batch of the ops queue that closed with items left. ops_batches gains
the thread opened for it (``thread_id``, cleared when the thread goes) and whether its cause key came back often
(``recurring``, criterion a10). agent_systems gains ``ops_failures``: how many ops runs in a row failed of their agent,
which pauses the system's ops at two (criterion a9).

Going back drops the new columns and puts the rules back; a thread of rule ``ops`` goes first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0241"
down_revision = "0240"
branch_labels = None
depends_on = None

THREADS = "orchestrator_threads"
RULES_BEFORE = ("failed", "stale", "escalate", "loop", "idle", "rounds", "budget")
RULES = (*RULES_BEFORE, "ops")
OUTCOMES = ("answer", "reassign", "ask_owner")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(value) for value in values)})"


def _rules(values: tuple[str, ...]) -> None:
    op.drop_constraint(f"{THREADS}_rule_check", THREADS, type_="check")
    op.create_check_constraint(f"{THREADS}_rule_check", THREADS, _in("rule", values))


def upgrade() -> None:
    _rules(RULES)
    op.add_column("ops_batches", sa.Column("thread_id", sa.BigInteger(), nullable=True))
    op.add_column("ops_batches", sa.Column("recurring", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_foreign_key(
        "ops_batches_thread_id_fkey", "ops_batches", THREADS, ["thread_id"], ["id"], ondelete="SET NULL"
    )
    op.add_column("agent_systems", sa.Column("ops_failures", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("agent_systems", "ops_failures")
    op.drop_constraint("ops_batches_thread_id_fkey", "ops_batches", type_="foreignkey")
    op.drop_column("ops_batches", "recurring")
    op.drop_column("ops_batches", "thread_id")
    op.execute(f"DELETE FROM {THREADS} WHERE rule = 'ops'")
    _rules(RULES_BEFORE)
