"""The merge checks of pull requests on github.com.

Revision ID: 0187
Revises: 0186
Create Date: 2026-10-30

merge_checks is new (plan super-board-parity-2): one row for each pull request on github.com whose run asked the hub to
judge it before it merges (``evo_agents.hub.server.merge_check``), in a repo whose policy has ``merge_check`` on. It
names the project, the repo and the pull request's number (one row each, ``merge_checks_project_id_repo_number_key``),
the run that asked with its plan and model, the pull request as the hub last read it, the head its verdict holds for
and the base the judge run merged into it, where the check stands (judging, passed or failed) and why, the Judges'
verdicts with their findings, the risk, the judge run in flight with its own key and how many ended without a verdict,
and the wait of the run that asked. The merge queue on GitLab keeps its own tables.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0187"
down_revision = "0186"
branch_labels = None
depends_on = None

TABLE = "merge_checks"
STATES = ("judging", "passed", "failed")


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    col = sa.column
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("web_url", sa.Text(), nullable=True),
        sa.Column("source_branch", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="judging", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("risk", postgresql.JSONB(), nullable=True),
        sa.Column("judge_run_id", sa.BigInteger(), nullable=True),
        sa.Column("judge_key", sa.Text(), nullable=True),
        sa.Column("judge_attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("project_id", "projects", ondelete="CASCADE"),
        _key("run_id", "runs", ondelete="SET NULL"),
        _key("judge_run_id", "runs", ondelete="SET NULL"),
        _key("decision_id", "decisions", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("project_id", "repo", "number", name=f"{TABLE}_project_id_repo_number_key"),
        sa.CheckConstraint(col("state").in_(STATES), name=f"{TABLE}_state_check"),
        sa.CheckConstraint(col("judge_attempts") >= 0, name=f"{TABLE}_judge_attempts_check"),
    )
    op.create_index(f"{TABLE}_state_idx", TABLE, ["state", "id"])
    op.create_index(f"{TABLE}_judge_run_idx", TABLE, ["judge_run_id"])


def downgrade() -> None:
    op.drop_table(TABLE)
