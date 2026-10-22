"""Credential sharing: credential_shares and credential_share_projects.

Revision ID: 0105
Revises: 0104
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.credential_shares`` and ``evo_agents.hub.tables_accounts`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

credential_shares holds each share a secret's owner gave of it (plan credential-sharing, criterion a1): the secret,
to whom (grantee_kind user with grantee_user_id, unit with grantee_unit_id, or all with neither), the autonomy level
a run using it is held to (A0 to A5, none for no ceiling of its own), a monthly budget in USD, a usage ceiling in
percent (1 to 100), the most runs that may hold it at once, when it ends, when its owner acknowledged the warning of
the terms of a personal subscription token, who created it, and when and by whom it was revoked. A share is never
deleted: revoking it sets revoked_at. credential_share_projects holds the projects a share is for, at least one, each
a project the secret is bound to when the share is made; that and who may write are the service's
(``evo_agents.hub.server.credential_shares``).

Going back drops the two tables; the shares are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

SHARES, PROJECTS = "credential_shares", "credential_share_projects"
GRANTEE_KINDS = ("user", "unit", "all")
LEVELS = ("A0", "A1", "A2", "A3", "A4", "A5")

col = sa.column


def _key(table: str, column: str, target: str, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _shares() -> None:
    kind = col("grantee_kind")
    op.create_table(
        SHARES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("secret_id", sa.BigInteger(), nullable=False),
        sa.Column("grantee_kind", sa.Text(), nullable=False),
        sa.Column("grantee_user_id", sa.BigInteger(), nullable=True),
        sa.Column("grantee_unit_id", sa.BigInteger(), nullable=True),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("budget_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("usage_ceiling_pct", sa.SmallInteger(), nullable=True),
        sa.Column("max_running", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terms_ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        _key(SHARES, "secret_id", "secrets", "CASCADE"),
        _key(SHARES, "grantee_user_id", "users", "CASCADE"),
        _key(SHARES, "grantee_unit_id", "org_units", "RESTRICT"),
        _key(SHARES, "created_by", "users", "RESTRICT"),
        _key(SHARES, "revoked_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{SHARES}_pkey"),
        sa.CheckConstraint(
            sa.and_(
                kind.in_(GRANTEE_KINDS),
                (kind == "user") == col("grantee_user_id").is_not(None),
                (kind == "unit") == col("grantee_unit_id").is_not(None),
                sa.or_(col("level").is_(None), col("level").in_(LEVELS)),
                sa.or_(col("budget_usd").is_(None), col("budget_usd") > 0),
                sa.or_(col("usage_ceiling_pct").is_(None), col("usage_ceiling_pct").between(1, 100)),
                sa.or_(col("max_running").is_(None), col("max_running") >= 1),
                col("revoked_at").is_(None) == col("revoked_by").is_(None),
            ),
            name=f"{SHARES}_check",
        ),
    )
    op.create_index(f"{SHARES}_secret_idx", SHARES, ["secret_id"], postgresql_where=col("revoked_at").is_(None))
    op.create_index(f"{SHARES}_user_idx", SHARES, ["grantee_user_id"])
    op.create_index(f"{SHARES}_unit_idx", SHARES, ["grantee_unit_id"])


def _projects() -> None:
    op.create_table(
        PROJECTS,
        sa.Column("share_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        _key(PROJECTS, "share_id", SHARES, "CASCADE"),
        _key(PROJECTS, "project_id", "projects", "CASCADE"),
        sa.PrimaryKeyConstraint("share_id", "project_id", name=f"{PROJECTS}_pkey"),
    )
    op.create_index(f"{PROJECTS}_project_idx", PROJECTS, ["project_id"])


def upgrade() -> None:
    _shares()
    _projects()


def downgrade() -> None:
    op.drop_table(PROJECTS)
    op.drop_table(SHARES)
