"""The Curator's changes: the judge run, what an accepted proposal became, and the repos whose ruleset was checked.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-08

``evo_agents.hub.judge`` and ``evo_agents.hub.server.changes`` describe the design; the lists and bounds below are
copies of their constants as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

runs.kind takes a fourth value, 'judge', the Curator's Judge of one change: a run of the plan the change is, with repos
as a plan run has, no step key or repo, that belongs to a schedule; a project has at most one judge run active at a
time (runs_active_judge_key).

curator_changes holds what an accepted proposal of tier 0 or 1 became: the plan on the hub, the one repo and the
branch (``curator/...``) it works on, the forge (github or gitlab), all four null only for a change left open because
its draft could not become a plan, the tier, where it stands (state, and why it stays open), its Builder's and its
Judge's runs, the pull request (number, url, base branch, the head commit judged), the Judge's verdict and whether it
passed, the check run the hub wrote, and the merge (when, and its commit).

curator_repo_checks holds, per repo of a project, the last check of its ruleset: the GitHub repo, its default branch,
whether a ruleset keeps the Curator's App off that branch and why, the rulesets read, when, and who asked (NULL: the
hub's own check again).

Going back deletes the judge runs, drops the two tables and the index, and puts the check of 0016 back, which is the
schema that schema 0016 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

KINDS_0016 = ("step", "plan", "review")
KINDS = ("step", "plan", "review", "judge")  # evo_agents.hub.runs.RUN_KINDS
ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")
STATES = ("planned", "pr_pending", "judge_pending", "judging", "judged", "merged", "open", "closed")
FORGES = ("github", "gitlab")  # evo_agents.hub.judge.FORGES
BRANCH_PREFIX = "curator/"  # evo_agents.hub.judge.BRANCH_PREFIX
MAX_REASON_CHARS = 2000
MAX_NAME_CHARS = 255
SHA = r"^[0-9a-f]{40}([0-9a-f]{24})?$"

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _line(name: str, most: int):
    """1 to ``most`` characters of ``name``, without a control character."""
    return sa.and_(sa.func.char_length(col(name)).between(1, most), ~col(name).op("~")(r"[\x01-\x1f\x7f]"))


def upgrade() -> None:
    # runs: the judge run
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.create_check_constraint("runs_kind_check", "runs", col("kind").in_(KINDS))
    op.create_check_constraint(
        "runs_judge_schedule_check", "runs", sa.or_(col("kind") != "judge", col("schedule_id").is_not(None))
    )
    op.create_index(
        "runs_active_judge_key",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == "judge", col("state").in_(ACTIVE)),
    )

    # the changes
    op.create_table(
        "curator_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("forge", sa.Text(), nullable=True),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.Text(), server_default="planned", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("builder_run_id", sa.BigInteger(), nullable=True),
        sa.Column("judge_run_id", sa.BigInteger(), nullable=True),
        sa.Column("judge_attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("base_branch", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("hidden_count", sa.SmallInteger(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("check_run_id", sa.BigInteger(), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_sha", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.CheckConstraint(col("state").in_(STATES), name="curator_changes_state_check"),
        sa.CheckConstraint(col("forge").in_(FORGES), name="curator_changes_forge_check"),
        sa.CheckConstraint(col("tier").between(0, 3), name="curator_changes_tier_check"),
        sa.CheckConstraint(
            sa.and_(_line("branch", MAX_NAME_CHARS), col("branch").startswith(BRANCH_PREFIX)),
            name="curator_changes_branch_check",
        ),
        sa.CheckConstraint(_line("repo", MAX_NAME_CHARS), name="curator_changes_repo_check"),
        sa.CheckConstraint(_line("reason", MAX_REASON_CHARS), name="curator_changes_reason_check"),
        sa.CheckConstraint(col("head_sha").op("~")(SHA), name="curator_changes_head_sha_check"),
        sa.CheckConstraint(col("merge_sha").op("~")(SHA), name="curator_changes_merge_sha_check"),
        sa.CheckConstraint(col("judge_attempts") >= 0, name="curator_changes_judge_attempts_check"),
        sa.CheckConstraint(
            sa.or_(
                col("state") == "open",
                sa.and_(
                    col("plan_id").is_not(None),
                    col("repo").is_not(None),
                    col("branch").is_not(None),
                    col("forge").is_not(None),
                ),
            ),
            name="curator_changes_planned_check",
        ),
        sa.CheckConstraint(col("pr_number") >= 1, name="curator_changes_pr_number_check"),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("verdict")) == "object", name="curator_changes_verdict_check"),
        sa.CheckConstraint(
            (col("state") == "merged") == col("merged_at").is_not(None), name="curator_changes_merged_check"
        ),
        sa.ForeignKeyConstraint(
            ["builder_run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_changes_builder_run_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["judge_run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_changes_judge_run_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_changes_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="curator_changes_proposal_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_changes_pkey"),
        sa.UniqueConstraint("proposal_id", name="curator_changes_proposal_id_key"),
        sa.UniqueConstraint("project_id", "plan_id", name="curator_changes_project_id_plan_id_key"),
    )
    op.create_index("curator_changes_state_idx", "curator_changes", ["state", "id"])

    # the repos whose ruleset was checked
    op.create_table(
        "curator_repo_checks",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("github_repo", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rulesets", postgresql.JSONB(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("checked_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(_line("reason", MAX_REASON_CHARS), name="curator_repo_checks_reason_check"),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("rulesets")) == "array", name="curator_repo_checks_rulesets_check"),
        sa.ForeignKeyConstraint(
            ["checked_by"], ["users.id"], ondelete="RESTRICT", name="curator_repo_checks_checked_by_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_repo_checks_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "repo", name="curator_repo_checks_pkey"),
    )


def downgrade() -> None:
    runs = sa.table("runs", col("kind"), col("attempt"))
    op.drop_table("curator_repo_checks")
    op.drop_index("curator_changes_state_idx", table_name="curator_changes")
    op.drop_table("curator_changes")
    op.drop_index("runs_active_judge_key", table_name="runs")
    op.drop_constraint("runs_judge_schedule_check", "runs", type_="check")
    for attempt in (3, 2, 1):  # the retries of a judge run name the attempt before them
        op.execute(runs.delete().where(runs.c.kind == "judge", runs.c.attempt == attempt))
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.create_check_constraint("runs_kind_check", "runs", col("kind").in_(KINDS_0016))
