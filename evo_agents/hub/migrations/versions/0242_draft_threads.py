"""The rule ``draft`` of orchestrator_threads: the thread the hub opens to the Orchestrator of an agent about a draft pull
request of its run's branch the hub could not settle once the run's trace ended (``evo_agents.hub.server.draft_pulls``).

Revision ID: 0242
Revises: 0241
Create Date: 2026-11-12

The names below are copies of ``evo_agents.hub.tables_orchestrator`` as of this revision, never imports, so a later
change to it takes a new revision.

Going back puts the rules back; a thread of rule ``draft`` goes first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None

THREADS = "orchestrator_threads"
RULES_BEFORE = ("failed", "stale", "escalate", "loop", "idle", "rounds", "budget", "ops")
RULES = (*RULES_BEFORE, "draft")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(value) for value in values)})"


def _rules(values: tuple[str, ...]) -> None:
    op.drop_constraint(f"{THREADS}_rule_check", THREADS, type_="check")
    op.create_check_constraint(f"{THREADS}_rule_check", THREADS, _in("rule", values))


def upgrade() -> None:
    _rules(RULES)


def downgrade() -> None:
    threads = sa.table(THREADS, sa.column("rule"))
    op.execute(threads.delete().where(threads.c.rule == "draft"))
    _rules(RULES_BEFORE)
