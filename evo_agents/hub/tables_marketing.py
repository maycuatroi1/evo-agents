"""The tables of the core plugin marketing (0119), which ``evo_agents.hub.tables`` adds to its metadata and exposes as
its own attributes (``tables.marketing_channels``, ``marketing_posts``, ``marketing_budget``, ``marketing_denylist``,
``marketing_state`` and ``marketing_metrics``): the channel accounts of a project, what was proposed to post on them
and what the gate decided (``docs/marketing-plugin.md``).

marketing_channels holds one row for each account of a channel: its kind (project or personal), its mode (autonomous
or draft_only), its adapter (api or browser), the member who owns a personal account, the target a post goes to (a
subreddit, a page id, a repo, an author URN) and when it was paused. No credential is in it: those are sealed in the
plugin's config. The constraints of the registry are checked here as well as in code: a personal account is never
autonomous but on facebook_profile and tiktok, facebook_profile is personal only, hackernews is always draft_only.

marketing_posts holds what an agent proposed: the text, its links and sources, the result of each layer of the gate,
the Judge's scores, where it ended (posted, draft with the reason, failed, withdrawn), its cost and the run that
proposed it. A post for a browser channel waits as queued until a worker's script claims it (posting, claimed_at) and
reports; its screenshot is kept on it (0120). marketing_budget holds the money reserved and spent in each month. marketing_denylist holds the terms,
domains and people a post must never name. marketing_state holds the kill switch of a project. marketing_metrics holds
the numbers read from a post at +24h and +7d.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

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
)
ACCOUNT_KINDS = ("project", "personal")
MODES = ("autonomous", "draft_only")
ADAPTERS = ("api", "browser")
PERSONAL_AUTONOMOUS = ("facebook_profile", "tiktok")  # the only channels a personal account may post on by itself
POST_KINDS = ("post", "reply")
POST_STATES = ("proposed", "judging", "queued", "posting", "posted", "draft", "failed", "withdrawn")
SESSION_STATES = ("none", "ok", "stale")  # the sealed login session of a browser channel
MAX_SHOT_BYTES = 2 * 1024 * 1024
DENY_KINDS = ("term", "domain", "person")
SPANS = ("24h", "7d")
MAX_TEXT = 20000


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _key(table: str, col: str, target: str, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([col], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{col}_fkey")


def channels_table(metadata: MetaData) -> Table:
    return Table(
        "marketing_channels",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("channel", Text, nullable=False),
        Column("account", Text, nullable=False),
        Column("account_kind", Text, nullable=False),
        Column("mode", Text, nullable=False, server_default="draft_only"),
        Column("adapter", Text, nullable=False, server_default="api"),
        Column("owner_id", BigInteger),
        Column("target", Text),
        Column("autonomous_since", DateTime(timezone=True)),
        Column("paused_at", DateTime(timezone=True)),
        Column("paused_reason", Text),
        Column("session_state", Text, nullable=False, server_default="none"),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        _key("marketing_channels", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_channels", "owner_id", "users", ondelete="SET NULL"),
        _key("marketing_channels", "created_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="marketing_channels_pkey"),
        UniqueConstraint("project_id", "channel", "account", name="marketing_channels_account_key"),
        Index("marketing_channels_project_idx", "project_id"),
    )


def posts_table(metadata: MetaData) -> Table:
    return Table(
        "marketing_posts",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("channel_id", BigInteger, nullable=False),
        Column("channel", Text, nullable=False),
        Column("account", Text, nullable=False),
        Column("kind", Text, nullable=False, server_default="post"),
        Column("reply_to", Text),
        Column("body", Text, nullable=False),
        Column("links", JSONB, nullable=False, server_default="[]"),
        Column("sources", JSONB, nullable=False, server_default="[]"),
        Column("state", Text, nullable=False, server_default="proposed"),
        Column("reason", Text),
        Column("layers", JSONB, nullable=False, server_default="{}"),
        Column("judge", JSONB),
        Column("question_id", BigInteger),
        Column("run_id", BigInteger),
        Column("proposed_by", BigInteger),
        Column("posted_url", Text),
        Column("external_id", Text),
        Column("cost_usd", Numeric(10, 4), nullable=False, server_default="0"),
        Column("reserved_usd", Numeric(10, 4), nullable=False, server_default="0"),
        Column("dry_run", Boolean, nullable=False, server_default="false"),
        _stamp("created_at"),
        Column("decided_at", DateTime(timezone=True)),
        Column("posted_at", DateTime(timezone=True)),
        Column("claimed_at", DateTime(timezone=True)),
        Column("screenshot", LargeBinary),
        _key("marketing_posts", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_posts", "channel_id", "marketing_channels", ondelete="CASCADE"),
        _key("marketing_posts", "proposed_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="marketing_posts_pkey"),
        Index("marketing_posts_project_idx", "project_id", "state"),
        Index("marketing_posts_channel_idx", "channel_id", "posted_at"),
    )


def budget_table(metadata: MetaData) -> Table:
    return Table(
        "marketing_budget",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("month", Text, nullable=False),
        Column("reserved_usd", Numeric(10, 4), nullable=False, server_default="0"),
        Column("spent_usd", Numeric(10, 4), nullable=False, server_default="0"),
        _key("marketing_budget", "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("project_id", "month", name="marketing_budget_pkey"),
    )


def denylist_table(metadata: MetaData) -> Table:
    return Table(
        "marketing_denylist",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("value", Text, nullable=False),
        Column("added_by", BigInteger),
        _stamp("added_at"),
        _key("marketing_denylist", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_denylist", "added_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="marketing_denylist_pkey"),
        UniqueConstraint("project_id", "kind", "value", name="marketing_denylist_value_key"),
    )


def state_table(metadata: MetaData) -> Table:
    return Table(
        "marketing_state",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("killed", Boolean, nullable=False, server_default="false"),
        Column("reason", Text),
        Column("killed_by", BigInteger),
        Column("killed_at", DateTime(timezone=True)),
        _key("marketing_state", "project_id", "projects", ondelete="CASCADE"),
        _key("marketing_state", "killed_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("project_id", name="marketing_state_pkey"),
    )


def metrics_table(metadata: MetaData) -> Table:
    return Table(
        "marketing_metrics",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("post_id", BigInteger, nullable=False),
        Column("span", Text, nullable=False),
        Column("metric", Text, nullable=False),
        Column("value", Numeric(14, 2), nullable=False),
        _stamp("taken_at"),
        Column("pushed_at", DateTime(timezone=True)),
        _key("marketing_metrics", "post_id", "marketing_posts", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="marketing_metrics_pkey"),
        UniqueConstraint("post_id", "span", "metric", name="marketing_metrics_once_key"),
    )


def marketing_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table, Table, Table]:
    """(marketing_channels, marketing_posts, marketing_budget, marketing_denylist, marketing_state, marketing_metrics),
    added to ``metadata``."""
    return (
        channels_table(metadata),
        posts_table(metadata),
        budget_table(metadata),
        denylist_table(metadata),
        state_table(metadata),
        metrics_table(metadata),
    )
