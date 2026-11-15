"""Credential shares for one run: credential_shares.run_id.

Revision ID: 0247
Revises: 0246
Create Date: 2026-11-15

An admin of a project who answers a credential request of another member's run with a value keeps it as their own
secret, and the hub lends it to that run alone, under a share to the run's principal for its project
(``evo_agents.hub.server.credential_answers``). credential_shares gains ``run_id``: the run such a share lends to, and
the runs that resume it (``runs.resume_of_run_id``); null for every share before this revision, which lends to every
run of its grantee in its projects. A share for one run names one member as its grantee
(credential_shares_run_grantee_check), and the shares of a run are looked up by it (credential_shares_run_idx). The key
sets null when its run goes, which a run never does.

Going back drops the column; a share for one run then lends to every run of its grantee in its projects, so it revokes
every such share not revoked first, as the admin who made it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0247"
down_revision = "0246"
branch_labels = None
depends_on = None

SHARES = "credential_shares"
CHECK = "credential_shares_run_grantee_check"
INDEX = "credential_shares_run_idx"
KEY = "credential_shares_run_id_fkey"

col = sa.column


def upgrade() -> None:
    op.add_column(SHARES, sa.Column("run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(KEY, SHARES, "runs", ["run_id"], ["id"], ondelete="SET NULL")
    op.create_check_constraint(CHECK, SHARES, sa.or_(col("run_id").is_(None), col("grantee_kind") == "user"))
    op.create_index(INDEX, SHARES, ["run_id"], postgresql_where=col("run_id").is_not(None))


def downgrade() -> None:
    shares = sa.table(
        SHARES,
        col("run_id", sa.BigInteger()),
        col("revoked_at", sa.DateTime(timezone=True)),
        col("revoked_by", sa.BigInteger()),
        col("created_by", sa.BigInteger()),
    )
    still = (shares.c.run_id.is_not(None), shares.c.revoked_at.is_(None))
    op.execute(sa.update(shares).where(*still).values(revoked_at=sa.func.now(), revoked_by=shares.c.created_by))
    op.drop_index(INDEX, table_name=SHARES)
    op.drop_column(SHARES, "run_id")
