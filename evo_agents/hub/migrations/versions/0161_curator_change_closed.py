"""A Curator change left open with no plan may be closed by hand.

Revision ID: 0161
Revises: 0160
Create Date: 2026-10-26

The names below are copies of ``evo_agents.hub.judge`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

curator_changes_planned_check (0017) let a change lack its plan, repo, branch or forge only while it is ``open``. An
admin of the project now closes such a change, one whose proposal named no repo say, with POST
.../curator/changes/{id}/close: a change that is ``closed`` may lack them too.

Going back puts the check of 0017 back, a change closed with no plan open again first, its reason kept.
"""

import sqlalchemy as sa
from alembic import op

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None

PLANNED_CHECK = "curator_changes_planned_check"
PLANLESS = ("open", "closed")  # the states of a change that may have no plan, repo, branch or forge

col = sa.column


def _planned_check(planless) -> None:
    """The check of curator_changes as 0017 wrote it, a change where ``planless`` holds free of a plan."""
    op.drop_constraint(PLANNED_CHECK, "curator_changes", type_="check")
    op.create_check_constraint(
        PLANNED_CHECK,
        "curator_changes",
        sa.or_(
            planless,
            sa.and_(
                col("plan_id").is_not(None),
                col("repo").is_not(None),
                col("branch").is_not(None),
                col("forge").is_not(None),
            ),
        ),
    )


def upgrade() -> None:
    _planned_check(col("state").in_(PLANLESS))


def downgrade() -> None:
    changes = sa.table("curator_changes", col("state"), col("plan_id"), col("repo"), col("branch"), col("forge"))
    planless = sa.or_(
        changes.c.plan_id.is_(None), changes.c.repo.is_(None), changes.c.branch.is_(None), changes.c.forge.is_(None)
    )
    op.execute(changes.update().where(changes.c.state == "closed", planless).values(state="open"))
    _planned_check(col("state") == "open")
