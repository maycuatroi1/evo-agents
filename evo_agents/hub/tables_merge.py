"""The tables of the hub's merge queue on GitLab, which ``evo_agents.hub.tables`` adds to its metadata and exposes as
its own attributes (``tables.merge_queue``, ``tables.merge_events``): queries name them there.

merge_queue (0068) holds one row for each merge request the queue took or the hub opened for a run: the project, the
repo and the merge request's iid, where it comes from (``merge_gate.SOURCES``: the run, the plan and its steps in the
repo, the Curator's change, the Builder's agent profile and model), where it stands (``merge_gate.STATES``) and why,
the head the gates last read and when the hub first saw it, the patch the Judge judged (``merge_gate.
patch_fingerprint``), the Judges' verdicts, the risk, the judge run in flight and its own key (a SHA-256), and what the
queue and what follows a merge fill in later (the time it became ready, the merge and the commits around it, its
rebases since its gates last passed, 0069). One merge request of a target branch has the turn at a time
(merge_queue_turn_key, 0069).
merge_events (0068) is its ledger, only ever added to: each gate passed or not with its reason, each verdict, and
what the queue, the release and the breaker do later (``merge_gate.EVENT_KINDS``).
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
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def _queue(metadata: MetaData) -> Table:
    table = Table(
        "merge_queue",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("repo", Text, nullable=False),
        Column("iid", Integer),  # null while the hub has not opened the merge request of its run
        Column("source", Text, nullable=False),
        Column("target_branch", Text, nullable=False),
        Column("source_branch", Text, nullable=False),
        Column("run_id", BigInteger),  # the run that made it: plan, step, task or the Curator's Builder
        Column("plan_id", Text),
        Column("steps", JSONB, nullable=False, server_default="[]"),
        Column("change_id", BigInteger),
        Column("profile", Text),  # the agent profile of the run that made it, as @handle
        Column("model", Text),  # the model of the run that made it; null for its runtime's default or a person
        Column("title", Text),
        Column("description", Text),  # the merge request's, as the gates last read it: the Judge reads it
        Column("web_url", Text),
        Column("state", Text, nullable=False, server_default="gating"),
        Column("reason", Text),
        Column("head_sha", Text),
        _when("head_at"),
        Column("fingerprint", Text),
        Column("verdict", JSONB),
        Column("risk", JSONB),
        Column("judge_run_id", BigInteger),
        Column("judge_key", Text),
        Column("judge_attempts", SmallInteger, nullable=False, server_default="0"),
        Column("commented", Text),  # the reason the hub last wrote on the merge request
        _when("queued_at"),
        _when("merged_at"),
        Column("before_sha", Text),
        Column("after_sha", Text),
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("rebases", SmallInteger, nullable=False, server_default="0"),  # since its gates last passed (0069)
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="merge_queue_project_id_fkey"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="merge_queue_run_id_fkey"),
        ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="SET NULL", name="merge_queue_change_id_fkey"
        ),
        ForeignKeyConstraint(["judge_run_id"], ["runs.id"], ondelete="SET NULL", name="merge_queue_judge_run_id_fkey"),
        PrimaryKeyConstraint("id", name="merge_queue_pkey"),
        UniqueConstraint("project_id", "repo", "iid", name="merge_queue_project_id_repo_iid_key"),
        Index("merge_queue_state_idx", "project_id", "state", "id"),
        Index("merge_queue_judge_run_idx", "judge_run_id"),
        Index("merge_queue_run_idx", "run_id"),
    )
    Index(  # one merge request for each plan and repo
        "merge_queue_plan_key",
        table.c.project_id,
        table.c.repo,
        table.c.plan_id,
        unique=True,
        postgresql_where=table.c.source == "plan",
    )
    Index(  # one merge request of a target branch has the turn (0069)
        "merge_queue_turn_key",
        table.c.project_id,
        table.c.repo,
        table.c.target_branch,
        unique=True,
        postgresql_where=table.c.state.in_(("rebasing", "testing", "merging")),
    )
    return table


def _events(metadata: MetaData) -> Table:
    return Table(
        "merge_events",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("queue_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("gate", SmallInteger),
        Column("passed", Boolean),
        Column("reason", Text),
        Column("sha", Text),
        Column("run_id", BigInteger),
        Column("detail", JSONB, nullable=False, server_default="{}"),
        _stamp("created_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="merge_events_project_id_fkey"),
        ForeignKeyConstraint(["queue_id"], ["merge_queue.id"], ondelete="CASCADE", name="merge_events_queue_id_fkey"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="merge_events_run_id_fkey"),
        PrimaryKeyConstraint("id", name="merge_events_pkey"),
        Index("merge_events_queue_idx", "queue_id", "id"),
        Index("merge_events_project_idx", "project_id", "id"),
    )


def merge_tables(metadata: MetaData) -> tuple[Table, Table]:
    """merge_queue and merge_events (0068), added to ``metadata``."""
    return _queue(metadata), _events(metadata)
