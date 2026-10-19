"""The tables of the Curator's changes, which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.curator_changes``, ``tables.curator_repo_checks``, ``tables.curator_ledger``,
``tables.replay_tasks``, ``tables.curator_replays``): queries name them there.

curator_changes (0017, 0019) holds what each accepted proposal became, curator_repo_checks (0017) the last check of
each repo's ruleset, and curator_ledger (0018) what happened to each proposal, never changed or deleted
(``evo_agents.hub.server.changes``, ``evo_agents.hub.server.ledger``). replay_tasks (0053) holds each project's suite
of replay tasks, made from its own history and only ever added to, and curator_replays (0053) each replay of one task
for one change, with what its two arms found (``evo_agents.hub.replay``, ``evo_agents.hub.server.replays``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def _changes(metadata: MetaData) -> Table:
    """An accepted proposal's plan, pull request, Judge and merge (0017)."""
    return Table(
        "curator_changes",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("proposal_id", BigInteger, nullable=False),
        Column("plan_id", Text),
        Column("repo", Text),
        Column("branch", Text),
        Column("forge", Text),
        Column("tier", SmallInteger, nullable=False),
        Column("state", Text, nullable=False, server_default="planned"),
        Column("reason", Text),
        Column("builder_run_id", BigInteger),
        Column("judge_run_id", BigInteger),
        Column("judge_attempts", SmallInteger, nullable=False, server_default="0"),
        Column("pr_number", Integer),
        Column("pr_url", Text),
        Column("base_branch", Text),
        Column("head_sha", Text),
        Column("hidden_count", SmallInteger),
        Column("verdict", JSONB),
        Column("passed", Boolean),
        Column("check_run_id", BigInteger),
        _when("merged_at"),
        Column("merge_sha", Text),
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("judge_key", Text),  # SHA-256 of the key the claim of its judge run handed the daemon (0019)
        ForeignKeyConstraint(
            ["builder_run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_changes_builder_run_id_fkey"
        ),
        ForeignKeyConstraint(
            ["judge_run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_changes_judge_run_id_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_changes_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="curator_changes_proposal_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="curator_changes_pkey"),
        UniqueConstraint("proposal_id", name="curator_changes_proposal_id_key"),
        UniqueConstraint("project_id", "plan_id", name="curator_changes_project_id_plan_id_key"),
        Index("curator_changes_state_idx", "state", "id"),
    )


def _repo_checks(metadata: MetaData) -> Table:
    """The last check of each repo's ruleset (0017)."""
    return Table(
        "curator_repo_checks",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("repo", Text, nullable=False),
        Column("github_repo", Text, nullable=False),
        Column("default_branch", Text),
        Column("protected", Boolean, nullable=False),
        Column("reason", Text, nullable=False),
        Column("rulesets", JSONB, nullable=False),
        Column("checked_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("checked_by", BigInteger),
        ForeignKeyConstraint(
            ["checked_by"], ["users.id"], ondelete="RESTRICT", name="curator_repo_checks_checked_by_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_repo_checks_project_id_fkey"
        ),
        PrimaryKeyConstraint("project_id", "repo", name="curator_repo_checks_pkey"),
    )


def _ledger(metadata: MetaData) -> Table:
    """What happened to each proposal, never changed or deleted; each merged change's outcome (0018)."""
    ledger = Table(
        "curator_ledger",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("proposal_id", BigInteger, nullable=False),
        Column("change_id", BigInteger),
        Column("action", Text, nullable=False),
        Column("actor", Text, nullable=False),
        Column("actor_id", BigInteger),
        Column("run_id", BigInteger),
        Column("what", Text, nullable=False),
        Column("commit_sha", Text),
        Column("before_sha", Text),
        Column("after_sha", Text),
        Column("figures", JSONB),
        Column("verdict", JSONB),
        Column("pr_url", Text),
        Column("pr_number", Integer),
        _when("merged_at"),
        Column("outcome", Text),
        Column("details", JSONB),
        _stamp("created_at"),
        ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="curator_ledger_actor_id_fkey"),
        ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_ledger_change_id_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_ledger_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="curator_ledger_proposal_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_ledger_run_id_fkey"),
        PrimaryKeyConstraint("id", name="curator_ledger_pkey"),
        Index("curator_ledger_proposal_idx", "proposal_id", "id"),
    )
    Index("curator_ledger_project_idx", ledger.c.project_id, ledger.c.created_at)
    Index(  # a merged change has one outcome
        "curator_ledger_outcome_key",
        ledger.c.change_id,
        unique=True,
        postgresql_where=ledger.c.action == "outcome",
    )
    return ledger


def _replay_tasks(metadata: MetaData) -> Table:
    """A project's suite of replay tasks, one per step of a plan done, only ever added to (0053). base_sha is set once,
    by the first replay that read it; unusable once a replay found the task's commits nowhere on origin."""
    return Table(
        "replay_tasks",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("plan_id", Text, nullable=False),
        Column("step_key", Text, nullable=False),
        Column("repo", Text, nullable=False),
        Column("title", Text),
        Column("what", Text, nullable=False),
        Column("verify", Text, nullable=False),
        Column("commits", ARRAY(Text), nullable=False),
        Column("source", Text, nullable=False),
        Column("split", Text, nullable=False),
        Column("label", JSONB, nullable=False),
        Column("base_sha", Text),
        Column("unusable", Text),
        _stamp("created_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="replay_tasks_project_id_fkey"),
        PrimaryKeyConstraint("id", name="replay_tasks_pkey"),
        UniqueConstraint("project_id", "plan_id", "step_key", name="replay_tasks_step_key"),
        Index("replay_tasks_split_idx", "project_id", "split", "id"),
    )


def _replays(metadata: MetaData) -> Table:
    """Each replay of one task for one change: its run, its own key, and what each arm found (0053)."""
    return Table(
        "curator_replays",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("change_id", BigInteger, nullable=False),
        Column("task_id", BigInteger, nullable=False),
        Column("run_id", BigInteger),
        Column("state", Text, nullable=False, server_default="queued"),
        Column("attempts", SmallInteger, nullable=False, server_default="0"),
        Column("replay_key", Text),  # SHA-256 of the key the claim of its run handed the daemon
        Column("base_sha", Text),
        Column("result_a", JSONB),
        Column("result_b", JSONB),
        Column("reason", Text),
        _stamp("created_at"),
        _stamp("updated_at"),
        ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_replays_change_id_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_replays_project_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_replays_run_id_fkey"),
        ForeignKeyConstraint(["task_id"], ["replay_tasks.id"], ondelete="CASCADE", name="curator_replays_task_id_fkey"),
        PrimaryKeyConstraint("id", name="curator_replays_pkey"),
        UniqueConstraint("change_id", "task_id", name="curator_replays_change_id_task_id_key"),
        Index("curator_replays_state_idx", "project_id", "state", "id"),
    )


def tables(metadata: MetaData, runs: Table, active: tuple[str, ...]) -> tuple[Table, Table, Table, Table, Table]:
    """curator_changes, curator_repo_checks, curator_ledger, replay_tasks and curator_replays, added to ``metadata``;
    and the index that keeps one replay run of a project active at a time (0053), as runs_active_judge_key does for
    judge runs: ``active`` are the states it counts (``tables.RUN_ACTIVE``)."""
    Index(
        "runs_active_replay_key",
        runs.c.project_id,
        unique=True,
        postgresql_where=(runs.c.kind == "replay") & runs.c.state.in_(active),
    )
    changes = _changes(metadata)
    return changes, _repo_checks(metadata), _ledger(metadata), _replay_tasks(metadata), _replays(metadata)
