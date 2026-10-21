"""Secrets an agent profile owns: the env secrets of the accounts an admin gives an agent.

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-21

Plan agent-accounts, criterion a2. secrets.owner_id says whose secret a row is, a member's; a row an agent profile owns
has no member owner and names its profile instead: profile_id is new, owner_id takes null, and
secrets_owner_check says each row has exactly one of the two. Every row there is stays a member's, with the key it had
(owner_id, name) unchanged: secrets_name_key does not move. A secret of an agent is unique by (profile_id, name) among
the rows not deleted (secrets_profile_name_key, the same partial shape), and is always kind env
(secrets_profile_env_check): git and forge secrets stay a member's. The names below are copies of
``evo_agents.hub.tables`` as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions.

The foreign key to agent_profiles restricts: profiles are soft-deleted, and a profile's secrets are emptied
(sealed value and bindings gone, deleted_at set) by the transaction that deletes it, not by the database.

Going back is refused while any row names a profile, deleted ones included: those rows have no owner a member's
secrets could give them, and the leases and runs that name them keep them. The error says so; delete the agent
accounts, and wait for no lease to name them, or restore a backup taken before this revision.
"""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

OWNER_CHECK = "secrets_owner_check"  # exactly one of owner_id and profile_id
ENV_CHECK = "secrets_profile_env_check"  # a profile's secret is kind env
FKEY = "secrets_profile_id_fkey"
NAME_KEY = "secrets_profile_name_key"

col = sa.column


def upgrade() -> None:
    op.add_column("secrets", sa.Column("profile_id", sa.BigInteger()))
    op.alter_column("secrets", "owner_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_foreign_key(FKEY, "secrets", "agent_profiles", ["profile_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint(OWNER_CHECK, "secrets", (col("owner_id").is_(None)) != (col("profile_id").is_(None)))
    op.create_check_constraint(ENV_CHECK, "secrets", sa.or_(col("profile_id").is_(None), col("kind") == "env"))
    op.create_index(
        NAME_KEY,
        "secrets",
        ["profile_id", "name"],
        unique=True,
        postgresql_where=col("deleted_at").is_(None),
    )


def downgrade() -> None:
    secrets = sa.table("secrets", col("profile_id"))
    held = op.get_bind().execute(
        sa.select(sa.func.count()).select_from(secrets).where(secrets.c.profile_id.is_not(None))
    )
    count = held.scalar_one()
    if count:
        raise RuntimeError(
            f"cannot go back to schema 0084: {count} secret row(s) belong to agent profiles, which schema 0084 has no "
            "owner for; delete the agent accounts (the rows stay as deleted ones while a lease or run names them) or "
            "restore a backup taken before 0085"
        )
    op.drop_index(NAME_KEY, table_name="secrets")
    op.drop_constraint(ENV_CHECK, "secrets", type_="check")
    op.drop_constraint(OWNER_CHECK, "secrets", type_="check")
    op.drop_constraint(FKEY, "secrets", type_="foreignkey")
    op.alter_column("secrets", "owner_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("secrets", "profile_id")
