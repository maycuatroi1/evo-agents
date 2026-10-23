"""The invite links of the hub: invites.

Revision ID: 0110
Revises: 0109
Create Date: 2026-10-22

Plan autonomous-oss-growth, criterion a1 (``evo_agents.hub.server.invites`` describes the design). A hub admin makes a
one-time link; a signed-in member accepts it. Kind join names a project, a role and a max level, and accepting it gives
that grant; kind register names none, and accepting it gives the right to register one new project, which the
registration that uses it marks with used_at and the project it made. for_login, when set, is the one login that may
accept the link. The hub keeps the hex SHA-256 of the link's token, never the token.

The names below are copies of ``evo_agents.hub.tables_invites`` and ``evo_agents.hub.access.ROLES`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions. No table that exists changes.

Going back drops the table: open links stop working, and the rights to register a project that were not used are lost;
the grants and projects they gave stay.
"""

import sqlalchemy as sa
from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None

KINDS = ("join", "register")
ROLES = ("reader", "writer", "admin")
LOGIN = "^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
SHA256 = "^[0-9a-f]{64}$"

col = sa.column


def _key(column: str, target: str, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"invites_{column}_fkey")


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _check():
    join, register = col("kind") == "join", col("kind") == "register"
    return sa.and_(
        col("kind").in_(KINDS),
        sa.or_(col("role").is_(None), col("role").in_(ROLES)),
        sa.or_(
            sa.and_(join, col("project_id").is_not(None), col("role").is_not(None), col("max_level").is_not(None)),
            sa.and_(register, col("role").is_(None), col("max_level").is_(None)),
        ),
        sa.or_(col("used_at").is_(None), sa.and_(register, col("accepted_at").is_not(None))),
        col("used_at").is_(None) == sa.or_(join, col("project_id").is_(None)),
        col("accepted_at").is_(None) == col("accepted_by").is_(None),
        col("revoked_at").is_(None) == col("revoked_by").is_(None),
        sa.or_(col("for_login").is_(None), col("for_login").regexp_match(LOGIN)),
        col("token_hash").regexp_match(SHA256),
        col("expires_at") > col("created_at"),
    )


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("max_level", sa.Text(), nullable=True),
        sa.Column("for_login", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _when("accepted_at"),
        sa.Column("accepted_by", sa.BigInteger(), nullable=True),
        _when("revoked_at"),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        _when("used_at"),
        _key("project_id", "projects", "CASCADE"),
        _key("created_by", "users", "RESTRICT"),
        _key("accepted_by", "users", "RESTRICT"),
        _key("revoked_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="invites_pkey"),
        sa.UniqueConstraint("token_hash", name="invites_token_hash_key"),
        sa.CheckConstraint(_check(), name="invites_check"),
    )
    op.create_index("invites_accepted_by_idx", "invites", ["accepted_by"])


def downgrade() -> None:
    op.drop_index("invites_accepted_by_idx", table_name="invites")
    op.drop_table("invites")
