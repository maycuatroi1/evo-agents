"""Quota pacing: the strategy quota-pacing, its thresholds, a policy of each account pool, and weekly budgets.

Revision ID: 0258
Revises: 0257
Create Date: 2026-11-16

Plan token-and-quota-economy, step 1. account_policies may name the strategy quota-pacing
(``evo_agents.hub.accounts.pick``) and gains its two thresholds: hard_reserve_pct, the share of an account's weekly
quota kept back while other accounts have more (0 to 100, 10 by default), and five_hour_ceiling_pct, the use of the
five-hour window above which an account is passed over while a cooler one exists (1 to 100, 90 by default). Its
strategy check is put back with the new strategy.

account_pools gains a policy of its own, which the agents of the pool follow in place of DEFAULT_POLICY: strategy
(round-robin by default, so each pool keeps the rotation it had) and the same two thresholds.

accounts gains weekly_budget_usd, the USD an owner gives a model account in a week when its provider has no quota API
(above 0, null for none), and weekly_reset_at, an instant the weeks of that budget start from, every 7 days before and
after it (null: Monday 00:00 UTC). The hub turns the cost the account's runs spent since the week began into its
seven_day window (``evo_agents.hub.quota``).

The values below are copies, never imports. Going back drops the new columns, moves every policy of quota-pacing to
round-robin and puts back 0027's strategy check.
"""

import sqlalchemy as sa
from alembic import op

revision = "0258"
down_revision = "0257"
branch_labels = None
depends_on = None

OLD_STRATEGIES = ("round-robin", "weighted-round-robin", "fill-first")
STRATEGIES = (*OLD_STRATEGIES, "quota-pacing")  # evo_agents.hub.accounts.STRATEGIES
STRATEGY_CHECK = "account_policies_strategy_check"
MAX_BUDGET_USD = 1_000_000

col = sa.column


def _thresholds(table: str) -> None:
    op.add_column(table, sa.Column("hard_reserve_pct", sa.SmallInteger(), server_default="10", nullable=False))
    op.add_column(table, sa.Column("five_hour_ceiling_pct", sa.SmallInteger(), server_default="90", nullable=False))
    op.create_check_constraint(f"{table}_hard_reserve_pct_check", table, col("hard_reserve_pct").between(0, 100))
    op.create_check_constraint(
        f"{table}_five_hour_ceiling_pct_check", table, col("five_hour_ceiling_pct").between(1, 100)
    )


def upgrade() -> None:
    op.drop_constraint(STRATEGY_CHECK, "account_policies", type_="check")
    op.create_check_constraint(STRATEGY_CHECK, "account_policies", col("strategy").in_(STRATEGIES))
    _thresholds("account_policies")
    op.add_column("account_pools", sa.Column("strategy", sa.Text(), server_default="round-robin", nullable=False))
    op.create_check_constraint("account_pools_strategy_check", "account_pools", col("strategy").in_(STRATEGIES))
    _thresholds("account_pools")
    op.add_column("accounts", sa.Column("weekly_budget_usd", sa.Numeric(12, 2), nullable=True))
    op.add_column("accounts", sa.Column("weekly_reset_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "accounts_weekly_budget_usd_check",
        "accounts",
        sa.or_(col("weekly_budget_usd").is_(None), col("weekly_budget_usd").between(0.01, MAX_BUDGET_USD)),
    )


def downgrade() -> None:
    op.drop_column("accounts", "weekly_reset_at")
    op.drop_column("accounts", "weekly_budget_usd")
    for name in ("five_hour_ceiling_pct", "hard_reserve_pct", "strategy"):
        op.drop_column("account_pools", name)
    for name in ("five_hour_ceiling_pct", "hard_reserve_pct"):
        op.drop_column("account_policies", name)
    op.drop_constraint(STRATEGY_CHECK, "account_policies", type_="check")
    policies = sa.table("account_policies", col("strategy"))
    op.execute(policies.update().where(col("strategy") == "quota-pacing").values(strategy="round-robin"))
    op.create_check_constraint(STRATEGY_CHECK, "account_policies", col("strategy").in_(OLD_STRATEGIES))
