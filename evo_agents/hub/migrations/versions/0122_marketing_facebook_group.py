"""The channel facebook_group of the core plugin marketing: a post for a Facebook group is a draft a member shares by
hand, never a post a machine makes (plan autonomous-oss-growth, step 20, and its non-goal on Facebook groups).

Revision ID: 0122
Revises: 0121
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_marketing`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

marketing_channels takes the channel facebook_group, for a member's personal account only and in draft_only mode
always. Going back deletes the facebook_group accounts (their posts go with them) and takes the check of 0119 again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

OLD_CHANNELS = (
    "github",
    "hackernews",
    "reddit",
    "x",
    "linkedin",
    "viblo",
    "facebook_page",
    "facebook_profile",
    "tiktok",
)
NEW_CHANNELS = (*OLD_CHANNELS, "facebook_group")  # evo_agents.hub.tables_marketing.CHANNELS
ACCOUNT_KINDS = ("project", "personal")
MODES = ("autonomous", "draft_only")
ADAPTERS = ("api", "browser")
PERSONAL_AUTONOMOUS = ("facebook_profile", "tiktok")

col = sa.column


def _check(channels: tuple[str, ...]) -> sa.ColumnElement:
    rules = [
        col("channel").in_(channels),
        col("account_kind").in_(ACCOUNT_KINDS),
        col("mode").in_(MODES),
        col("adapter").in_(ADAPTERS),
        sa.or_(col("account_kind") == "project", col("owner_id").is_not(None)),
        sa.or_(col("account_kind") == "project", col("mode") == "draft_only", col("channel").in_(PERSONAL_AUTONOMOUS)),
        sa.or_(col("channel") != "facebook_profile", col("account_kind") == "personal"),
        sa.or_(col("channel") != "hackernews", col("mode") == "draft_only"),
    ]
    if "facebook_group" in channels:
        rules.append(
            sa.or_(
                col("channel") != "facebook_group",
                sa.and_(col("account_kind") == "personal", col("mode") == "draft_only"),
            )
        )
    return sa.and_(*rules)


def _swap(channels: tuple[str, ...]) -> None:
    op.drop_constraint("marketing_channels_check", "marketing_channels", type_="check")
    op.create_check_constraint("marketing_channels_check", "marketing_channels", _check(channels))


def upgrade() -> None:
    _swap(NEW_CHANNELS)


def downgrade() -> None:
    channels = sa.table("marketing_channels", col("channel"))
    op.execute(channels.delete().where(channels.c.channel == "facebook_group"))
    _swap(OLD_CHANNELS)
