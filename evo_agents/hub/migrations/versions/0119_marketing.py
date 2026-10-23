"""The channel accounts and the posting gate of the core plugin marketing: marketing_channels, marketing_posts,
marketing_budget, marketing_denylist, marketing_state and marketing_metrics.

Revision ID: 0119
Revises: 0118
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_marketing`` as of this revision, never imports, so a later
change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A channel account is one row of marketing_channels: its channel, kind (project or personal), mode (autonomous or
draft_only) and adapter (api or browser). The CHECK keeps the registry's rules in the database as well: a personal
account has an owner and is autonomous only on facebook_profile and tiktok, facebook_profile is personal only,
hackernews is always draft_only. A post (marketing_posts) keeps the text, the result of each layer of the gate, the
Judge's scores, the cost and where it ended; it goes with its channel. marketing_budget holds the money reserved and
spent in each month, marketing_denylist the terms, domains and people a post must never name, marketing_state the kill
switch of a project, marketing_metrics the numbers read from a post at +24h and +7d. No credential is in any of them.

Going back drops the six tables; the accounts, the posts and their audit are lost.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None

CHANNELS = (
    "github",
    "hackernews",
    "reddit",
    "x",
    "linkedin",
    "viblo",
    "facebook_page",
    "facebook_profile",
    "tiktok",
)  # evo_agents.hub.tables_marketing.CHANNELS
ACCOUNT_KINDS = ("project", "personal")
MODES = ("autonomous", "draft_only")
ADAPTERS = ("api", "browser")
PERSONAL_AUTONOMOUS = ("facebook_profile", "tiktok")
POST_KINDS = ("post", "reply")
POST_STATES = ("proposed", "judging", "posted", "draft", "failed", "withdrawn")
DENY_KINDS = ("term", "domain", "person")
SPANS = ("24h", "7d")
MAX_TEXT = 20000

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def _channels() -> None:
    op.create_table(
        "marketing_channels",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("account_kind", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), server_default="draft_only", nullable=False),
        sa.Column("adapter", sa.Text(), server_default="api", nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("autonomous_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _key("marketing_channels", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_channels", "owner_id", "users", ondelete="SET NULL"),
        _key("marketing_channels", "created_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="marketing_channels_pkey"),
        sa.UniqueConstraint("project_id", "channel", "account", name="marketing_channels_account_key"),
        sa.CheckConstraint(
            sa.and_(
                col("channel").in_(CHANNELS),
                col("account_kind").in_(ACCOUNT_KINDS),
                col("mode").in_(MODES),
                col("adapter").in_(ADAPTERS),
                sa.or_(col("account_kind") == "project", col("owner_id").is_not(None)),
                sa.or_(
                    col("account_kind") == "project",
                    col("mode") == "draft_only",
                    col("channel").in_(PERSONAL_AUTONOMOUS),
                ),
                sa.or_(col("channel") != "facebook_profile", col("account_kind") == "personal"),
                sa.or_(col("channel") != "hackernews", col("mode") == "draft_only"),
            ),
            name="marketing_channels_check",
        ),
    )
    op.create_index("marketing_channels_project_idx", "marketing_channels", ["project_id"])


def _posts() -> None:
    op.create_table(
        "marketing_posts",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="post", nullable=False),
        sa.Column("reply_to", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("links", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("sources", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("state", sa.Text(), server_default="proposed", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("layers", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("judge", postgresql.JSONB(), nullable=True),
        sa.Column("question_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("proposed_by", sa.BigInteger(), nullable=True),
        sa.Column("posted_url", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), server_default="0", nullable=False),
        sa.Column("reserved_usd", sa.Numeric(10, 4), server_default="0", nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.false(), nullable=False),
        _stamp("created_at"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        _key("marketing_posts", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_posts", "channel_id", "marketing_channels", ondelete="CASCADE"),
        _key("marketing_posts", "proposed_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="marketing_posts_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(POST_KINDS),
                col("state").in_(POST_STATES),
                sa.func.char_length(col("body")).between(1, MAX_TEXT),
            ),
            name="marketing_posts_check",
        ),
    )
    op.create_index("marketing_posts_project_idx", "marketing_posts", ["project_id", "state"])
    op.create_index("marketing_posts_channel_idx", "marketing_posts", ["channel_id", "posted_at"])


def upgrade() -> None:
    _channels()
    _posts()
    op.create_table(
        "marketing_budget",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(10, 4), server_default="0", nullable=False),
        sa.Column("spent_usd", sa.Numeric(10, 4), server_default="0", nullable=False),
        _key("marketing_budget", "project_id", "projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "month", name="marketing_budget_pkey"),
    )
    op.create_table(
        "marketing_denylist",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        _stamp("added_at"),
        _key("marketing_denylist", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_denylist", "added_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="marketing_denylist_pkey"),
        sa.UniqueConstraint("project_id", "kind", "value", name="marketing_denylist_value_key"),
        sa.CheckConstraint(
            sa.and_(col("kind").in_(DENY_KINDS), sa.func.char_length(col("value")).between(1, 200)),
            name="marketing_denylist_check",
        ),
    )
    op.create_table(
        "marketing_state",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("killed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("killed_by", sa.BigInteger(), nullable=True),
        sa.Column("killed_at", sa.DateTime(timezone=True), nullable=True),
        _key("marketing_state", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_state", "killed_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", name="marketing_state_pkey"),
    )
    op.create_table(
        "marketing_metrics",
        _id(),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("span", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(14, 2), nullable=False),
        _stamp("taken_at"),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        _key("marketing_metrics", "post_id", "marketing_posts", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="marketing_metrics_pkey"),
        sa.UniqueConstraint("post_id", "span", "metric", name="marketing_metrics_once_key"),
        sa.CheckConstraint(col("span").in_(SPANS), name="marketing_metrics_check"),
    )


def downgrade() -> None:
    op.drop_table("marketing_metrics")
    op.drop_table("marketing_state")
    op.drop_table("marketing_denylist")
    op.drop_table("marketing_budget")
    op.drop_index("marketing_posts_channel_idx", table_name="marketing_posts")
    op.drop_index("marketing_posts_project_idx", table_name="marketing_posts")
    op.drop_table("marketing_posts")
    op.drop_index("marketing_channels_project_idx", table_name="marketing_channels")
    op.drop_table("marketing_channels")
