"""The browser adapter of the core plugin marketing: a login session on each channel, a queue for the worker's script and
the screenshot of a post.

Revision ID: 0126
Revises: 0125
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_marketing`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

marketing_channels gains session_state (none, ok or stale): whether the sealed login session of a browser channel
works. marketing_posts takes two states, queued (the gate passed a post for a browser channel and a worker's script has
not claimed it) and posting (a script claimed it and has not reported), and gains claimed_at and screenshot (the PNG the
script took after posting, or when it stopped). No session is in any column: it is a sealed field of the plugin's config.

Going back turns the posts that wait or run for a browser into drafts, takes the check of 0125 again and drops the
three columns; the screenshots are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

OLD_STATES = ("proposed", "judging", "posted", "draft", "failed", "withdrawn")
NEW_STATES = ("proposed", "judging", "queued", "posting", "posted", "draft", "failed", "withdrawn")
POST_KINDS = ("post", "reply")
SESSION_STATES = ("none", "ok", "stale")
MAX_TEXT = 20000

col = sa.column


def _posts_check(states: tuple[str, ...]) -> sa.ColumnElement:
    return sa.and_(
        col("kind").in_(POST_KINDS),
        col("state").in_(states),
        sa.func.char_length(col("body")).between(1, MAX_TEXT),
    )


def _swap(states: tuple[str, ...]) -> None:
    op.drop_constraint("marketing_posts_check", "marketing_posts", type_="check")
    op.create_check_constraint("marketing_posts_check", "marketing_posts", _posts_check(states))


def upgrade() -> None:
    op.add_column("marketing_channels", sa.Column("session_state", sa.Text(), server_default="none", nullable=False))
    op.create_check_constraint(
        "marketing_channels_session_check", "marketing_channels", col("session_state").in_(SESSION_STATES)
    )
    op.add_column("marketing_posts", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("marketing_posts", sa.Column("screenshot", sa.LargeBinary(), nullable=True))
    _swap(NEW_STATES)


def downgrade() -> None:
    posts = sa.table("marketing_posts", col("state"), col("reason"))
    op.execute(
        posts.update()
        .where(posts.c.state.in_(("queued", "posting")))
        .values(state="draft", reason="a person posts it: the browser adapter was removed")
    )
    _swap(OLD_STATES)
    op.drop_column("marketing_posts", "screenshot")
    op.drop_column("marketing_posts", "claimed_at")
    op.drop_constraint("marketing_channels_session_check", "marketing_channels", type_="check")
    op.drop_column("marketing_channels", "session_state")
