"""A comment of a BA document anchored to a passage of an item, and the run of @ba a comment asked to answer.

Revision ID: 0227
Revises: 0226
Create Date: 2026-11-09

The names below are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to
it takes a new revision.

srs_comments takes what anchors a thread to a passage of an item: the part of the item it is on (``field``: the body,
a field's name, the steps), the passage as the reader selected it (``quote``), a little of the text before and after
it (``prefix``, ``suffix``) and where it started in that part (``start_offset``), all null for a comment on a whole
item, version or document; and ``ask_run_id``, the task run of @ba the comment mentioned to answer the thread. A
comment's version is the one it was made on: a later version finds the passage again by its words, or says it changed.

Going back drops the columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0227"
down_revision = "0226"
branch_labels = None
depends_on = None

TABLE = "srs_comments"
MAX_FIELD_CHARS = 100
MAX_QUOTE_CHARS = 2_000
MAX_CONTEXT_CHARS = 200

col = sa.column


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("field", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("quote", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("prefix", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("suffix", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("start_offset", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("ask_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(f"{TABLE}_ask_run_id_fkey", TABLE, "runs", ["ask_run_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint(
        f"{TABLE}_anchor_check",
        TABLE,
        sa.and_(
            sa.or_(col("field").is_(None), sa.func.char_length(col("field")) <= MAX_FIELD_CHARS),
            sa.or_(
                col("quote").is_(None),
                sa.and_(sa.func.char_length(col("quote")).between(1, MAX_QUOTE_CHARS), col("item_id").is_not(None)),
            ),
            sa.or_(col("prefix").is_(None), sa.func.char_length(col("prefix")) <= MAX_CONTEXT_CHARS),
            sa.or_(col("suffix").is_(None), sa.func.char_length(col("suffix")) <= MAX_CONTEXT_CHARS),
            sa.or_(col("start_offset").is_(None), col("start_offset") >= 0),
        ),
    )


def downgrade() -> None:
    op.drop_constraint(f"{TABLE}_anchor_check", TABLE, type_="check")
    op.drop_constraint(f"{TABLE}_ask_run_id_fkey", TABLE, type_="foreignkey")
    for column in ("ask_run_id", "start_offset", "suffix", "prefix", "quote", "field"):
        op.drop_column(TABLE, column)
