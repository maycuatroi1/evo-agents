"""A dismissal of the review queue's seventh rule, conflicting_claim.

Revision ID: 0199
Revises: 0198
Create Date: 2026-11-04

The names below are copies of ``evo_agents.kg.review`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The review queue gains a seventh rule, ``conflicting_claim`` (plan kg-entity-graph, criterion a6): two claims of one
subject and relation that two documents state with different objects. kg_review_dismissals_check (0141) takes it
among the rules a dismissal names, so a writer can dismiss such an item; its other bounds are as 0141 wrote them.

Going back deletes the dismissals of that rule (the next build lists their items again, with nobody to dismiss them
on a hub of that revision) and restores the check of 0141.
"""

import sqlalchemy as sa
from alembic import op

revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None

TABLE = "kg_review_dismissals"
CHECK = "kg_review_dismissals_check"
OLD_RULES = (
    "dangling_mention",
    "unmentioned_document",
    "cluster_without_knowledge",
    "possible_duplicate",
    "ambiguous_match",
    "bulk_noise",
)
NEW_RULE = "conflicting_claim"
MAX_REASON_CHARS = 1000

col = sa.column


def _check(rules: tuple[str, ...]) -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(
        CHECK,
        TABLE,
        sa.and_(
            col("rule").in_(rules),
            sa.func.char_length(col("fingerprint")).between(1, 100),
            sa.func.char_length(sa.func.btrim(col("reason"))).between(1, MAX_REASON_CHARS),
        ),
    )


def upgrade() -> None:
    _check((*OLD_RULES, NEW_RULE))


def downgrade() -> None:
    op.execute(sa.table(TABLE, col("rule")).delete().where(col("rule") == NEW_RULE))
    _check(OLD_RULES)
