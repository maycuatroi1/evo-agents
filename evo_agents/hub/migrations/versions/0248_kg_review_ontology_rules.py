"""A dismissal of the review queue's five ontology rules.

Revision ID: 0248
Revises: 0247
Create Date: 2026-11-15

The names below are copies of ``evo_agents.kg.review`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The review queue gains the rules a project's ontology states (``evo_agents.kg.ontology_rules``, plan
kg-centralized-on-hub, criterion a2): ``wrong_endpoint``, ``cardinality``, ``cycle``, ``duplicate_key`` and
``missing_prop``. kg_review_dismissals_check (0199) takes them among the rules a dismissal names, so a writer can
dismiss such an item; its other bounds are as 0141 wrote them.

Going back deletes the dismissals of those rules (the next build lists their items again, with nobody to dismiss
them on a hub of that revision) and restores the check of 0199.
"""

import sqlalchemy as sa
from alembic import op

revision = "0248"
down_revision = "0247"
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
    "conflicting_claim",
)
NEW_RULES = ("wrong_endpoint", "cardinality", "cycle", "duplicate_key", "missing_prop")
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
    _check((*OLD_RULES, *NEW_RULES))


def downgrade() -> None:
    op.execute(sa.table(TABLE, col("rule")).delete().where(col("rule").in_(NEW_RULES)))
    _check(OLD_RULES)
