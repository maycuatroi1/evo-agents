"""The emails of members: each address belongs to one member, once on the hub, whatever its case.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-21

user_emails holds each address a member listed, or a hub admin listed for them (``evo_agents.hub.server.user_emails``),
as it was written, with when. The unique index on lower(email) keeps an address to one member whatever its case; the
rows of a member go with the member. A GitHub noreply address names its login and needs no row
(``evo_agents.hub.user_emails``). The values below are copies of that module's constants as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions, as ``evo_agents.hub.tables_accounts`` describes the table.

Going back drops the table and its rows: schema 0067 keeps no email of a member.
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

TABLE = "user_emails"
MAX_EMAIL_CHARS = 254  # evo_agents.hub.user_emails.MAX_EMAIL_CHARS
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"  # evo_agents.hub.user_emails.EMAIL

col = sa.column


def upgrade() -> None:
    email = col("email")
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            sa.and_(sa.func.char_length(email) <= MAX_EMAIL_CHARS, email.op("~")(EMAIL)), name="user_emails_email_check"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="user_emails_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="user_emails_pkey"),
    )
    op.create_index("user_emails_email_key", TABLE, [sa.func.lower(email)], unique=True)
    op.create_index("user_emails_user_idx", TABLE, ["user_id"])


def downgrade() -> None:
    op.drop_index("user_emails_user_idx", table_name=TABLE)
    op.drop_index("user_emails_email_key", table_name=TABLE)
    op.drop_table(TABLE)
