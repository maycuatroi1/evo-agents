"""The growth rules of the core plugins marketing and crm on the Loop: a post tied to its claim and Signal key and
posted at a time, the replies to a project account's posts, and the invite links @community sent.

Revision ID: 0121
Revises: 0120
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_marketing`` and ``evo_agents.hub.tables_crm`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

marketing_posts gains claim_id (the canvas claim a post of a campaign tests), signal_key (the key its numbers count
toward), scheduled_at (the gate decides it then, not before) and released_at (when the Loop's publish work item let
it through). marketing_replies holds what someone answered under a post of a project account: the platform's id of
the reply, its author, link and text, once each. crm_invites_sent holds each invite link @community made for an
invite request: the request, the login it is bound to, the link and the run, once per request; it is what the weekly
cap counts.

Going back drops the two tables and the four columns; scheduled posts that did not go out become drafts first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

MAX_REPLY_TEXT = 4000

col = sa.column


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _when(name: str, *, now: bool = False) -> sa.Column:
    if now:
        return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.add_column("marketing_posts", sa.Column("claim_id", sa.BigInteger(), nullable=True))
    op.add_column("marketing_posts", sa.Column("signal_key", sa.Text(), nullable=True))
    op.add_column("marketing_posts", _when("scheduled_at"))
    op.add_column("marketing_posts", _when("released_at"))
    op.create_foreign_key(
        "marketing_posts_claim_id_fkey", "marketing_posts", "canvas_claims", ["claim_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("marketing_posts_scheduled_idx", "marketing_posts", ["project_id", "scheduled_at"])
    op.create_table(
        "marketing_replies",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        _when("seen_at", now=True),
        _key("marketing_replies", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_replies", "post_id", "marketing_posts", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="marketing_replies_pkey"),
        sa.UniqueConstraint("post_id", "external_id", name="marketing_replies_once_key"),
        sa.CheckConstraint(sa.func.char_length(col("body")).between(1, MAX_REPLY_TEXT), name="marketing_replies_check"),
    )
    op.create_index("marketing_replies_project_idx", "marketing_replies", ["project_id", "seen_at"])
    op.create_table(
        "crm_invites_sent",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("invite_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _when("sent_at", now=True),
        _key("crm_invites_sent", "project_id", "projects", ondelete="CASCADE"),
        _key("crm_invites_sent", "invite_id", "invites", ondelete="SET NULL"),
        _key("crm_invites_sent", "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="crm_invites_sent_pkey"),
        sa.UniqueConstraint("project_id", "ref", name="crm_invites_sent_ref_key"),
    )
    op.create_index("crm_invites_sent_week_idx", "crm_invites_sent", ["project_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("crm_invites_sent_week_idx", table_name="crm_invites_sent")
    op.drop_table("crm_invites_sent")
    op.drop_index("marketing_replies_project_idx", table_name="marketing_replies")
    op.drop_table("marketing_replies")
    posts = sa.table("marketing_posts", col("state"), col("reason"), col("scheduled_at"), col("released_at"))
    op.execute(
        posts.update()
        .where(posts.c.state == "proposed", posts.c.scheduled_at.is_not(None), posts.c.released_at.is_(None))
        .values(state="draft", reason="a person posts it: the schedule of posts was removed")
    )
    op.drop_index("marketing_posts_scheduled_idx", table_name="marketing_posts")
    op.drop_constraint("marketing_posts_claim_id_fkey", "marketing_posts", type_="foreignkey")
    for name in ("released_at", "scheduled_at", "signal_key", "claim_id"):
        op.drop_column("marketing_posts", name)
