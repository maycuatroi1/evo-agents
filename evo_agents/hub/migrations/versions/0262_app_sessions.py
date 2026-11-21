"""App sessions: first-party apps that sign their own users in, and the identities they give them.

Revision ID: 0262
Revises: 0261
Create Date: 2026-11-21

Plan evo-creative-workspace, step 1. A hub admin registers a first-party app under a secret the hub seals
(auth_apps); the app trades the stable identity its enterprise sign-in gives a person for an interactive session of
that member, at POST /v1/auth/app-session (``evo_agents.hub.server.app_sessions``). app_identities ties each app and
subject to one users row, created with no GitHub account (``github_id`` NULL). The session is a row of tokens of kind
web whose host is ``app:<name>``: no column of tokens changes.

Both tables start empty. Going back revokes every app session still live, then drops the two tables; the members they
made keep their rows and grants, and sign in no more until an admin gives them another way.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0262"
down_revision = "0261"
branch_labels = None
depends_on = None

APP_HOST_PREFIX = "app:"  # evo_agents.hub.server.app_sessions.HOST_PREFIX


def upgrade() -> None:
    op.create_table(
        "auth_apps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sealed_secret", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="auth_apps_pkey"),
        sa.UniqueConstraint("name", name="auth_apps_name_key"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="auth_apps_created_by_fkey"),
    )
    op.create_table(
        "app_identities",
        sa.Column("app_id", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("app_id", "subject", name="app_identities_pkey"),
        sa.ForeignKeyConstraint(["app_id"], ["auth_apps.id"], ondelete="CASCADE", name="app_identities_app_id_fkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="app_identities_user_id_fkey"),
    )
    op.create_index("app_identities_user_id_idx", "app_identities", ["user_id"])


def downgrade() -> None:
    tokens = sa.table(
        "tokens", sa.column("kind", sa.Text), sa.column("host", sa.Text), sa.column("revoked_at", sa.DateTime)
    )
    live_app_sessions = sa.and_(
        tokens.c.kind == "web", tokens.c.host.startswith(APP_HOST_PREFIX), tokens.c.revoked_at.is_(None)
    )
    op.execute(sa.update(tokens).where(live_app_sessions).values(revoked_at=sa.func.now()))
    op.drop_index("app_identities_user_id_idx", table_name="app_identities")
    op.drop_table("app_identities")
    op.drop_table("auth_apps")
