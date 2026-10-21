"""Accounts: a member's env secrets as accounts a run picks among, the member's policy for each project, and the
account each run took.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-13

``evo_agents.hub.accounts`` describes the design; the lists and bounds below are copies of its constants as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

accounts holds, for an env secret its owner set something on or a run took, its weight (0 to 1000, 1 by default) and
priority (0 to 1000, 0 by default), when its owner paused it, until when it cools down and why, when a run last took
it, and its rotation, the place a stride scheduler gives it; a row goes with its secret. A secret without a row has
the defaults, which is what every secret has after this revision: nothing that exists changes meaning.

account_policies holds a member's policy for a project: strategy (round-robin, weighted-round-robin or fill-first),
cooldown_seconds (60 seconds to 7 days), failover and max_failovers (0 to 20). A member without a row has
round-robin, an hour, failover on and 3 failovers.

run_accounts holds the account a run took for each variable, and how (manual, worker, charter, pool or cooling), so
the run keeps it on every later ask for its leases; failovers counts the times the run moved to another one. A row
goes with its run; a secret a run took is never deleted, only soft-deleted, so it keeps it.

Going back drops the three tables, which is the schema that schema 0026 runs on: its runs take the first env secret
by name for each variable, as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

STRATEGIES = ("round-robin", "weighted-round-robin", "fill-first")  # evo_agents.hub.accounts.STRATEGIES
CHOICES = ("manual", "worker", "charter", "pool", "cooling")  # evo_agents.hub.accounts.CHOICES
MAX_WEIGHT = 1000
MAX_PRIORITY = 1000
MIN_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 7 * 24 * 3600
MAX_FAILOVERS = 20
MAX_REASON_CHARS = 2000
MAX_ENV_VAR_CHARS = 200

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("secret_id", sa.BigInteger(), nullable=False),
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        _when("paused_at"),
        _when("cooldown_until"),
        sa.Column("cooldown_reason", sa.Text(), nullable=True),
        _when("last_leased_at"),
        sa.Column("rotation", sa.Double(), server_default="0", nullable=False),
        _stamp("updated_at"),
        sa.CheckConstraint(col("weight").between(0, MAX_WEIGHT), name="accounts_weight_check"),
        sa.CheckConstraint(col("priority").between(0, MAX_PRIORITY), name="accounts_priority_check"),
        sa.CheckConstraint(
            sa.func.char_length(col("cooldown_reason")).between(1, MAX_REASON_CHARS),
            name="accounts_cooldown_reason_check",
        ),
        sa.CheckConstraint(col("rotation") >= 0, name="accounts_rotation_check"),
        sa.ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="CASCADE", name="accounts_secret_id_fkey"),
        sa.PrimaryKeyConstraint("secret_id", name="accounts_pkey"),
    )
    op.create_table(
        "account_policies",
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.Text(), server_default="round-robin", nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("failover", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("max_failovers", sa.SmallInteger(), server_default="3", nullable=False),
        _stamp("updated_at"),
        sa.CheckConstraint(col("strategy").in_(STRATEGIES), name="account_policies_strategy_check"),
        sa.CheckConstraint(
            col("cooldown_seconds").between(MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS),
            name="account_policies_cooldown_check",
        ),
        sa.CheckConstraint(col("max_failovers").between(0, MAX_FAILOVERS), name="account_policies_failovers_check"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="account_policies_owner_id_fkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="account_policies_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("owner_id", "project_id", name="account_policies_pkey"),
    )
    op.create_table(
        "run_accounts",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("env_var", sa.Text(), nullable=False),
        sa.Column("secret_id", sa.BigInteger(), nullable=False),
        sa.Column("how", sa.Text(), nullable=False),
        _stamp("chosen_at"),
        sa.Column("failovers", sa.SmallInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(
            sa.func.char_length(col("env_var")).between(1, MAX_ENV_VAR_CHARS), name="run_accounts_env_var_check"
        ),
        sa.CheckConstraint(col("how").in_(CHOICES), name="run_accounts_how_check"),
        sa.CheckConstraint(col("failovers") >= 0, name="run_accounts_failovers_check"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_accounts_run_id_fkey"),
        sa.ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="RESTRICT", name="run_accounts_secret_id_fkey"),
        sa.PrimaryKeyConstraint("run_id", "env_var", name="run_accounts_pkey"),
    )
    op.create_index("run_accounts_secret_idx", "run_accounts", ["secret_id"])


def downgrade() -> None:
    op.drop_index("run_accounts_secret_idx", table_name="run_accounts")
    op.drop_table("run_accounts")
    op.drop_table("account_policies")
    op.drop_table("accounts")
