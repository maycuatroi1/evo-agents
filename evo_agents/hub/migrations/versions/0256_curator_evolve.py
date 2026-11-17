"""The Curator's evolutions of a harness, its changes in several repos, and its playbook's own retires.

Revision ID: 0256
Revises: 0255
Create Date: 2026-11-16

Plan pareto-curator-evolution, step 1. curator_evolve_runs holds each evolution of a harness (the files of a repo, or
an agent of an agent system), curator_evolve_candidates each candidate it measured with its whole text, and
curator_evolve_arm_cache the result of a baseline arm of one task, read again rather than run again
(``evo_agents.hub.evolve``). curator_change_repos holds each repo of a Curator's change that works in several, with its
own pull request and Judge. curator_playbook gains the proposal an entry holds until (until_proposal_id) and the count
of review runs that showed it (prompt_exposures), and curator_playbook_deltas the actor of a delta (``curator`` for the
hub's maintenance). The tables start empty and the columns null or 0. Going back drops them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0256"
down_revision = "0255"
branch_labels = None
depends_on = None


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def _project(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{table}_project_id_fkey")


def _playbook() -> None:
    op.add_column("curator_playbook", sa.Column("until_proposal_id", sa.BigInteger(), nullable=True))
    op.add_column("curator_playbook", sa.Column("prompt_exposures", sa.Integer(), server_default="0", nullable=False))
    op.create_foreign_key(
        "curator_playbook_until_proposal_id_fkey",
        "curator_playbook",
        "proposals",
        ["until_proposal_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("curator_playbook_deltas", sa.Column("actor", sa.Text(), nullable=True))


def _change_repos() -> None:
    op.create_table(
        "curator_change_repos",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("change_id", sa.BigInteger(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("depends_on", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("forge", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="pr_pending", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("judge_run_id", sa.BigInteger(), nullable=True),
        sa.Column("judge_attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("judge_key", sa.Text(), nullable=True),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("check_run_id", sa.BigInteger(), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_sha", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _project("curator_change_repos"),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_change_repos_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["judge_run_id"], ["runs.id"], ondelete="SET NULL", name="curator_change_repos_judge_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_change_repos_pkey"),
        sa.UniqueConstraint("change_id", "repo", name="curator_change_repos_change_id_repo_key"),
    )
    op.create_index("curator_change_repos_state_idx", "curator_change_repos", ["project_id", "state", "id"])


def _evolve_runs() -> None:
    op.create_table(
        "curator_evolve_runs",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("paths", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("system_id", sa.BigInteger(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("evolve_key", sa.Text(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("budget", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("tasks", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("iterations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("frontier", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("best_candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        sa.Column("change_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _project("curator_evolve_runs"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="curator_evolve_runs_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="SET NULL", name="curator_evolve_runs_proposal_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="SET NULL", name="curator_evolve_runs_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL", name="curator_evolve_runs_created_by_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_evolve_runs_pkey"),
    )
    op.create_index("curator_evolve_runs_state_idx", "curator_evolve_runs", ["project_id", "state", "id"])
    op.create_index("curator_evolve_runs_run_idx", "curator_evolve_runs", ["run_id"])


def _evolve_candidates() -> None:
    op.create_table(
        "curator_evolve_candidates",
        _id(),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("evolve_id", sa.BigInteger(), nullable=False),
        sa.Column("parents", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Integer(), server_default="0", nullable=False),
        sa.Column("components", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("scores", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6, asdecimal=False), nullable=True),
        sa.Column("turns", sa.Integer(), nullable=True),
        sa.Column("traces", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("on_front", sa.Boolean(), server_default="false", nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        _project("curator_evolve_candidates"),
        sa.ForeignKeyConstraint(
            ["evolve_id"],
            ["curator_evolve_runs.id"],
            ondelete="CASCADE",
            name="curator_evolve_candidates_evolve_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="curator_evolve_candidates_pkey"),
        sa.UniqueConstraint("evolve_id", "digest", name="curator_evolve_candidates_evolve_id_digest_key"),
    )


def _evolve_arm_cache() -> None:
    op.create_table(
        "curator_evolve_arm_cache",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("task_kind", sa.Text(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("baseline_digest", sa.Text(), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _project("curator_evolve_arm_cache"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], ondelete="SET NULL", name="curator_evolve_arm_cache_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "key", name="curator_evolve_arm_cache_pkey"),
    )


def upgrade() -> None:
    _playbook()
    _change_repos()
    _evolve_runs()
    _evolve_candidates()
    _evolve_arm_cache()


def downgrade() -> None:
    op.drop_table("curator_evolve_arm_cache")
    op.drop_table("curator_evolve_candidates")
    op.drop_table("curator_evolve_runs")
    op.drop_table("curator_change_repos")
    op.drop_column("curator_playbook_deltas", "actor")
    op.drop_constraint("curator_playbook_until_proposal_id_fkey", "curator_playbook", type_="foreignkey")
    op.drop_column("curator_playbook", "prompt_exposures")
    op.drop_column("curator_playbook", "until_proposal_id")
