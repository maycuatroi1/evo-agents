"""Settle chats: a chat the hub opens with an agent that every message of a person wakes, and the settle chat of each
version of a BA document whose review left conflicts for members to settle.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-21

chats.kind takes ``settle`` (``evo_agents.hub.chats.CHAT_KINDS``): a chat the hub opens, kept as a group chat is,
whose agent (``direct_profile_id``) every message of a person wakes without an @. It names no person
(``direct_user_id`` NULL), so chats_direct_check takes it, and chats_direct_key, whose NULL person never clashes, holds
any number of them. chat_mentions_via_check takes ``settle``, how such a message reached the chat's agent.

srs_settle_chats holds the settle chat of each version of a BA document of the core plugin srs whose review by @ba
ended with findings of level decide still open (``evo_agents.core_plugins.srs.settle``): one chat a version and one
version a chat, with the chat's first message, the review run whose end opened it and the chat run its first message
started. srs_findings.commit_authors holds the author emails of the newest commits of the files a finding's sources
name, as the run that recorded it read the knowledge graph: whom the settle chat invites for those sources. No route
returns it.

The names below are copies of ``evo_agents.hub.chats`` and ``evo_agents.hub.tables_srs`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

Going back makes every settle chat a group chat of the same members, its agent no longer named on its row, and a
mention that reached its agent ``via`` settle reads as ``via`` thread, how a message without an @ reached an agent
before; it drops srs_settle_chats and srs_findings.commit_authors.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

GROUP, DIRECT, SETTLE = "group", "direct", "settle"  # evo_agents.hub.chats.CHAT_KINDS
VIA_0050 = ("login", "handle", "project", "thread", DIRECT)
VIA = (*VIA_0050, SETTLE)  # evo_agents.hub.chats.VIA
TABLE, FINDINGS = "srs_settle_chats", "srs_findings"
MAX_COMMIT_AUTHORS = 60  # evo_agents.hub.tables_srs.MAX_COMMIT_AUTHORS

col = sa.column


def _key(column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def _create_via(values) -> None:
    rule = sa.or_(col("via").is_(None), col("via").in_(values))
    op.create_check_constraint("chat_mentions_via_check", "chat_mentions", rule)


def _drop_kinds() -> None:
    op.drop_constraint("chats_direct_check", "chats", type_="check")
    op.drop_constraint("chats_kind_check", "chats", type_="check")


def _create_kinds(kinds) -> None:
    """chats_kind_check and chats_direct_check for ``kinds``: a group chat names neither person nor agent, a direct
    chat both, a settle chat its agent alone."""
    op.create_check_constraint("chats_kind_check", "chats", col("kind").in_(kinds))
    user, profile = col("direct_user_id"), col("direct_profile_id")
    pairs = [
        sa.and_(col("kind") == GROUP, user.is_(None), profile.is_(None)),
        sa.and_(col("kind") == DIRECT, user.is_not(None), profile.is_not(None)),
    ]
    if SETTLE in kinds:
        pairs.append(sa.and_(col("kind") == SETTLE, user.is_(None), profile.is_not(None)))
    op.create_check_constraint("chats_direct_check", "chats", sa.or_(*pairs))


def upgrade() -> None:
    _drop_kinds()
    _create_kinds((GROUP, DIRECT, SETTLE))
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    _create_via(VIA)
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("review_run_id", sa.BigInteger(), nullable=True),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("version_id", "srs_versions", ondelete="CASCADE"),
        _key("chat_id", "chats", ondelete="CASCADE"),
        _key("root_message_id", "chat_messages", ondelete="CASCADE"),
        _key("review_run_id", "runs", ondelete="SET NULL"),
        _key("agent_run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="srs_settle_chats_pkey"),
        sa.UniqueConstraint("version_id", name="srs_settle_chats_version_id_key"),
        sa.UniqueConstraint("chat_id", name="srs_settle_chats_chat_id_key"),
    )
    op.add_column(
        FINDINGS,
        sa.Column("commit_authors", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    authors = col("commit_authors")
    op.create_check_constraint(
        "srs_findings_commit_authors_check",
        FINDINGS,
        sa.and_(
            sa.func.jsonb_typeof(authors) == "array",
            sa.func.jsonb_array_length(authors) <= MAX_COMMIT_AUTHORS,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("srs_findings_commit_authors_check", FINDINGS, type_="check")
    op.drop_column(FINDINGS, "commit_authors")
    op.drop_table(TABLE)
    op.drop_constraint("chat_mentions_via_check", "chat_mentions", type_="check")
    mentions = sa.table("chat_mentions", col("via"))
    op.execute(sa.update(mentions).where(mentions.c.via == SETTLE).values(via="thread"))
    _create_via(VIA_0050)
    _drop_kinds()
    chats = sa.table("chats", col("kind"), col("direct_profile_id"))
    op.execute(sa.update(chats).where(chats.c.kind == SETTLE).values(kind=GROUP, direct_profile_id=None))
    _create_kinds((GROUP, DIRECT))
