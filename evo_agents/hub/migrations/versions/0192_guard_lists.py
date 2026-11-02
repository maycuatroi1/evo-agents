"""Guard: the private names of a project and the repos it publishes.

Revision ID: 0192
Revises: 0191
Create Date: 2026-11-02

guard_terms and guard_targets are new, the tables of the core plugin guard (``docs/guard.md``). A term is a private name
of a project (1 to 200 characters), kept with its words as ``evo_agents.private_names`` reads them (unique in the
project) and an optional note of at most 200 characters. A target is a pattern of the repos the project publishes,
``host/owner/repo`` in lower case with ``*`` and ``?`` (1 to 200 characters, unique in the project).

Only tables are added. Going back drops them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0192"
down_revision = "0191"
branch_labels = None
depends_on = None

TERMS, TARGETS = "guard_terms", "guard_targets"
col = sa.column


def _create(name: str, value: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column(value, sa.Text(), nullable=False),
        *([sa.Column("words", sa.Text(), nullable=False)] if value == "term" else []),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"{name}_project_id_fkey", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=f"{name}_created_by_fkey", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        sa.UniqueConstraint("project_id", "words" if value == "term" else value, name=f"{name}_key"),
        sa.CheckConstraint(sa.func.char_length(col(value)).between(1, 200), name=f"{name}_{value}_check"),
        sa.CheckConstraint(
            sa.or_(col("note").is_(None), sa.func.char_length(col("note")) <= 200), name=f"{name}_note_check"
        ),
    )


def upgrade() -> None:
    _create(TERMS, "term")
    _create(TARGETS, "pattern")


def downgrade() -> None:
    op.drop_table(TARGETS)
    op.drop_table(TERMS)
