"""The contacts of the core plugin crm: crm_contacts, crm_touchpoints, crm_stage_history, crm_cursors and crm_deletions.

Revision ID: 0124
Revises: 0123
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_crm`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A contact is a GitHub login that found the project itself (plan autonomous-oss-growth, criteria a13, a21 and a22): its
public profile link, the source it was first seen from, its stage (lead, invited, activated, retained), the label of the
row and the times. Its touchpoints and the stages it reached go with it when it is deleted, which the CASCADE keys
do. crm_cursors keeps how far each source was read; crm_deletions keeps the number of a deleted contact and how many
touchpoints went, and no login. No email address is stored anywhere.

Going back drops the five tables; the contacts and their history are lost.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

STAGES = ("lead", "invited", "activated", "retained")  # evo_agents.hub.tables_crm.STAGES
SOURCES = ("invite_request", "invite_link", "grant", "issue", "pull_request", "discussion")
KINDS = (*SOURCES, "note")
MAX_NOTE_CHARS = 1000

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "crm_contacts",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("profile_url", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), server_default="lead", nullable=False),
        sa.Column("label", postgresql.JSONB(), server_default="{}", nullable=False),
        _stamp("first_seen_at"),
        _stamp("stage_at"),
        _stamp("created_at"),
        _key("crm_contacts", "project_id", "projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="crm_contacts_pkey"),
        sa.UniqueConstraint("project_id", "login", name="crm_contacts_login_key"),
        sa.CheckConstraint(
            sa.and_(
                col("source").in_(SOURCES),
                col("stage").in_(STAGES),
                col("login") == sa.func.lower(col("login")),
                sa.func.char_length(col("login")).between(1, 39),
            ),
            name="crm_contacts_check",
        ),
    )
    op.create_index("crm_contacts_stage_idx", "crm_contacts", ["project_id", "stage"])
    op.create_table(
        "crm_touchpoints",
        _id(),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), server_default="", nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("author_id", sa.BigInteger(), nullable=True),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=False),
        _stamp("created_at"),
        _key("crm_touchpoints", "contact_id", "crm_contacts", ondelete="CASCADE"),
        _key("crm_touchpoints", "author_id", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="crm_touchpoints_pkey"),
        sa.UniqueConstraint("contact_id", "kind", "ref", name="crm_touchpoints_once_key"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                sa.func.char_length(col("note")) <= MAX_NOTE_CHARS,
                (col("kind") == "note") == col("note").is_not(None),
            ),
            name="crm_touchpoints_check",
        ),
    )
    op.create_index("crm_touchpoints_author_idx", "crm_touchpoints", ["author_id"])
    op.create_table(
        "crm_stage_history",
        _id(),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("reached_at", sa.DateTime(timezone=True), nullable=False),
        _stamp("created_at"),
        _key("crm_stage_history", "contact_id", "crm_contacts", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="crm_stage_history_pkey"),
        sa.UniqueConstraint("contact_id", "stage", name="crm_stage_history_once_key"),
        sa.CheckConstraint(col("stage").in_(STAGES), name="crm_stage_history_check"),
    )
    op.create_table(
        "crm_cursors",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        _stamp("updated_at"),
        _key("crm_cursors", "project_id", "projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "source", name="crm_cursors_pkey"),
    )
    op.create_table(
        "crm_deletions",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("touchpoints", sa.Integer(), nullable=False),
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
        _stamp("deleted_at"),
        _key("crm_deletions", "project_id", "projects", ondelete="CASCADE"),
        _key("crm_deletions", "deleted_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="crm_deletions_pkey"),
    )
    op.create_index("crm_deletions_project_idx", "crm_deletions", ["project_id"])


def downgrade() -> None:
    op.drop_index("crm_deletions_project_idx", table_name="crm_deletions")
    op.drop_table("crm_deletions")
    op.drop_table("crm_cursors")
    op.drop_table("crm_stage_history")
    op.drop_index("crm_touchpoints_author_idx", table_name="crm_touchpoints")
    op.drop_table("crm_touchpoints")
    op.drop_index("crm_contacts_stage_idx", table_name="crm_contacts")
    op.drop_table("crm_contacts")
