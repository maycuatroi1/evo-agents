"""The opportunity a goal answers, a claim links, and a work item of the Loop goes from.

Revision ID: 0145
Revises: 0144
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_goals``, ``tables_canvas`` and ``tables_loop`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

canvas_claims gains ``opportunities`` (plan opportunity-intake, criterion a5): the codes (O-<number>) of opportunities
of the project the claim links, an array of at most 20 texts beside ``use_cases``, ``signal_keys`` and ``problems``,
empty for every claim made before. The check of 0135 takes the new column in. A rev's content is JSON, so it holds the
list with no change of canvas_claim_revs; a rev written before reads as linking no opportunity.

goal_revisions gains ``opportunity_id``: the opportunity of the goal's project the revision answers, or NULL. A
revision answers at most one problem or one opportunity, never both (check). Its key has no ON DELETE action, so a
project's goals and opportunities go in one delete, and an opportunity a revision names is not deleted under it.

loop_items gains ``opportunity_id``: the opportunity its goal answered when the engine made it, or what the work item
it comes of carried; NULL for every work item made before. Its key goes to opportunities with ON DELETE SET NULL, and
an index holds the work items of each opportunity.

Going back drops all three columns, the key, the index and the check, and puts the check of 0135 back; the revs of a
claim keep their opportunities.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None

MAX_LINKS = 20  # content.MAX_LINKS
LINKS_0135 = ("use_cases", "signal_keys", "problems")
OPPORTUNITIES = "opportunities"
CLAIMS = "canvas_claims"
CHECK = "canvas_claims_links_check"
GOALS = "goal_revisions"
GOAL_KEY = "goal_revisions_opportunity_id_fkey"
GOAL_CHECK = "goal_revisions_one_answer_check"
ITEMS = "loop_items"
ITEM_KEY = "loop_items_opportunity_id_fkey"
ITEM_INDEX = "loop_items_opportunity_idx"
COLUMN = "opportunity_id"

col = sa.column


def _check(names) -> None:
    op.drop_constraint(CHECK, CLAIMS, type_="check")
    op.create_check_constraint(CHECK, CLAIMS, sa.and_(*(sa.func.cardinality(col(name)) <= MAX_LINKS for name in names)))


def upgrade() -> None:
    op.add_column(CLAIMS, sa.Column(OPPORTUNITIES, postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False))
    _check((*LINKS_0135, OPPORTUNITIES))
    op.add_column(GOALS, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    op.create_foreign_key(GOAL_KEY, GOALS, OPPORTUNITIES, [COLUMN], ["id"])
    op.create_check_constraint(GOAL_CHECK, GOALS, sa.or_(col("problem_id").is_(None), col(COLUMN).is_(None)))
    op.add_column(ITEMS, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    op.create_foreign_key(ITEM_KEY, ITEMS, OPPORTUNITIES, [COLUMN], ["id"], ondelete="SET NULL")
    op.create_index(ITEM_INDEX, ITEMS, [COLUMN], postgresql_where=col(COLUMN).is_not(None))


def downgrade() -> None:
    op.drop_index(ITEM_INDEX, table_name=ITEMS)
    op.drop_constraint(ITEM_KEY, ITEMS, type_="foreignkey")
    op.drop_column(ITEMS, COLUMN)
    op.drop_constraint(GOAL_CHECK, GOALS, type_="check")
    op.drop_constraint(GOAL_KEY, GOALS, type_="foreignkey")
    op.drop_column(GOALS, COLUMN)
    _check(LINKS_0135)
    op.drop_column(CLAIMS, OPPORTUNITIES)
