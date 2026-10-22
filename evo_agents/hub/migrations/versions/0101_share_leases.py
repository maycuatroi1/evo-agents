"""Leases under a credential share: credential_leases.principal_id and share_id, run_accounts.how "shared".

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_accounts`` and ``evo_agents.hub.accounts`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

credential_leases gains principal_id, the member a lease was made for (plan credential-sharing, criterion a4): the
member who dispatched the run, since runs.principal_id of plan autonomy-levels is not in the schema yet. Each lease
there is now gets its run's dispatched_by. share_id names the share a lease of another member's secret was made
under (``evo_agents.hub.server.share_leases``), null for a lease of the principal's own secret or of a GitHub token;
the leases under a share are looked up by it (credential_leases_share_idx). Both keys set null
when their row goes, which a users row merged at sign-in and a share never does. run_accounts.how takes "shared",
for an account a run took under a share.

Going back moves each "shared" choice to "pool", the tier it would have come from, and drops the two columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

LEASES, RUN_ACCOUNTS = "credential_leases", "run_accounts"
OLD_CHOICES = ("manual", "worker", "charter", "pool", "cooling")
CHOICES = (*OLD_CHOICES, "shared")
HOW_CHECK = "run_accounts_how_check"

col = sa.column


def upgrade() -> None:
    op.add_column(LEASES, sa.Column("principal_id", sa.BigInteger(), nullable=True))
    op.add_column(LEASES, sa.Column("share_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(f"{LEASES}_principal_id_fkey", LEASES, "users", ["principal_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(
        f"{LEASES}_share_id_fkey", LEASES, "credential_shares", ["share_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(
        f"{LEASES}_share_idx",
        LEASES,
        ["share_id"],
        postgresql_where=col("share_id").is_not(None),
    )
    leases = sa.table(LEASES, col("run_id"), col("principal_id"))
    runs = sa.table("runs", col("id"), col("dispatched_by"))
    op.execute(
        leases.update()
        .values(principal_id=runs.c.dispatched_by)
        .where(runs.c.id == leases.c.run_id, leases.c.principal_id.is_(None))
    )
    op.drop_constraint(HOW_CHECK, RUN_ACCOUNTS, type_="check")
    op.create_check_constraint(HOW_CHECK, RUN_ACCOUNTS, col("how").in_(CHOICES))


def downgrade() -> None:
    taken = sa.table(RUN_ACCOUNTS, col("how"))
    op.execute(taken.update().values(how="pool").where(taken.c.how == "shared"))
    op.drop_constraint(HOW_CHECK, RUN_ACCOUNTS, type_="check")
    op.create_check_constraint(HOW_CHECK, RUN_ACCOUNTS, col("how").in_(OLD_CHOICES))
    op.drop_index(f"{LEASES}_share_idx", table_name=LEASES)
    op.drop_constraint(f"{LEASES}_share_id_fkey", LEASES, type_="foreignkey")
    op.drop_constraint(f"{LEASES}_principal_id_fkey", LEASES, type_="foreignkey")
    op.drop_column(LEASES, "share_id")
    op.drop_column(LEASES, "principal_id")
