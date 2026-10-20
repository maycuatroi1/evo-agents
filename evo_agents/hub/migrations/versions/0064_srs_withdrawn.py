"""A version of a BA document in review can be withdrawn: srs_versions.state takes 'withdrawn'.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-20

The author of a version in review, or an admin of its project, withdraws it (``evo_agents.core_plugins.srs.reviews``,
``withdraw``). Withdrawn is a last state, as superseded is: the version is never submitted, reviewed or approved again,
and approving a later version leaves it withdrawn. srs_versions_check takes it as a fifth state, which carries
decided_at, the moment it was withdrawn, as an approved version carries the moment it was approved. The names below
are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to them takes a new
revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back makes every withdrawn version a draft again, without decided_at, and puts back the check of 0046, which is
the schema that schema 0063 runs on.
"""

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

TABLE, CHECK = "srs_versions", "srs_versions_check"
VERSION_STATES_0063 = ("draft", "in_review", "approved", "superseded")
WITHDRAWN = "withdrawn"
VERSION_STATES = (*VERSION_STATES_0063, WITHDRAWN)  # evo_agents.hub.tables_srs.VERSION_STATES
MAX_SUMMARY_CHARS = 500
SHA256_CHARS = 64

col = sa.column


def _check(states: tuple[str, ...]) -> None:
    """srs_versions_check of 0046 over ``states``; a withdrawn version, where ``states`` holds it, has decided_at."""
    state = col("state")
    conditions = [
        col("number") >= 1,
        col("parent_number") < col("number"),
        state.in_(states),
        col("round") >= 0,
        sa.func.char_length(col("summary")) <= MAX_SUMMARY_CHARS,
        sa.func.char_length(col("content_sha256")) == SHA256_CHARS,
        sa.or_(state != "approved", col("decided_at").is_not(None)),
        sa.or_(state != "in_review", col("submitted_at").is_not(None)),
    ]
    if WITHDRAWN in states:
        conditions.append(sa.or_(state != WITHDRAWN, col("decided_at").is_not(None)))
    op.create_check_constraint(CHECK, TABLE, sa.and_(*conditions))


def upgrade() -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    _check(VERSION_STATES)


def downgrade() -> None:
    versions = sa.table(TABLE, col("state"), col("decided_at"))
    op.execute(versions.update().where(versions.c.state == WITHDRAWN).values(state="draft", decided_at=None))
    op.drop_constraint(CHECK, TABLE, type_="check")
    _check(VERSION_STATES_0063)
