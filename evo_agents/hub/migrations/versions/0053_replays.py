"""The Curator's replays: each project's suite of replay tasks made from its own history, and runs of kind replay.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-19

``evo_agents.hub.replay`` holds the model and ``evo_agents.hub.server.replays`` the routes and the night shift's part;
the values below are copies of their constants and of ``evo_agents.hub.runs`` as of this revision, never imports, so a
later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables_curator`` describes the tables.

replay_tasks holds a project's suite: one row per step of a plan that is done, has a verify of one line and names its
commits (in its evidence, else a run of it reported one), with its repo, title, what and verify as the plan had them
when the job ``curator.replay_suite`` read it, its commits, where they were read, its split (``select`` or
``holdout``, fixed by the SHA-256 of its plan and step), the plan's label, and once a replay read it, its base commit,
the first parent of the oldest of its commits; ``unusable`` says why no replay can run it. A task is never changed
otherwise, nor deleted while its project lives.

curator_replays holds each replay of one task for one change of the Curator: its run, how many runs it took, the
SHA-256 of the key its run's claim handed the daemon, its base commit, and what arm ``a`` (the harness of the default
branch) and arm ``b`` (the harness of the change's branch) found, an object each.

runs.kind takes ``replay``: a run of the Curator only the night shift queues (``runs_replay_schedule_check``), on the
plan of the change it replays, one active at a time per project (``runs_active_replay_key``).

Going back deletes the replay runs and drops the two tables, the index and the checks, which is the schema that schema
0052 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

KINDS_0052 = ("step", "plan", "review", "judge", "author", "chat", "local", "task")
KINDS = (*KINDS_0052, "replay")  # evo_agents.hub.runs.SHOWN_KINDS
ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")
SPLITS = ("select", "holdout")  # evo_agents.hub.replay.SPLITS
SOURCES = ("evidence", "run")  # evo_agents.hub.replay.SOURCES
STATES = ("queued", "running", "done", "failed")  # evo_agents.hub.replay.STATES
MAX_COMMITS = 20  # evo_agents.hub.replay.MAX_COMMITS
MAX_WHAT_BYTES = 16 * 1024  # evo_agents.hub.replay.MAX_WHAT_BYTES
MAX_TITLE_CHARS = 300  # evo_agents.hub.replay.MAX_TITLE_CHARS
MAX_VERIFY_CHARS = 2000  # evo_agents.hub.replay.MAX_VERIFY_CHARS
MAX_REASON_CHARS = 2000  # evo_agents.hub.replay.MAX_REASON_CHARS
MAX_ATTEMPTS = 10
MAX_NAME_CHARS = 255
SHA = r"^[0-9a-f]{40}([0-9a-f]{24})?$"

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _line(name: str, most: int):
    """1 to ``most`` characters of ``name``, without a control character."""
    return sa.and_(sa.func.char_length(col(name)).between(1, most), ~col(name).op("~")(r"[\x01-\x1f\x7f]"))


def _result(name: str):
    return sa.or_(col(name).is_(None), sa.func.jsonb_typeof(col(name)) == "object")


def _kinds(kinds) -> None:
    op.drop_constraint("runs_kind_check", "runs", type_="check")
    op.create_check_constraint("runs_kind_check", "runs", col("kind").in_(kinds))


def _create_tasks() -> None:
    op.create_table(
        "replay_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("what", sa.Text(), nullable=False),
        sa.Column("verify", sa.Text(), nullable=False),
        sa.Column("commits", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("unusable", sa.Text(), nullable=True),
        _stamp("created_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="replay_tasks_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="replay_tasks_pkey"),
        sa.UniqueConstraint("project_id", "plan_id", "step_key", name="replay_tasks_step_key"),
        sa.CheckConstraint(
            sa.and_(
                _line("plan_id", MAX_NAME_CHARS),
                _line("step_key", MAX_NAME_CHARS),
                _line("repo", MAX_NAME_CHARS),
                sa.or_(col("title").is_(None), sa.func.char_length(col("title")).between(1, MAX_TITLE_CHARS)),
                sa.func.octet_length(col("what")).between(1, MAX_WHAT_BYTES),
                _line("verify", MAX_VERIFY_CHARS),
                sa.func.cardinality(col("commits")).between(1, MAX_COMMITS),
                col("source").in_(SOURCES),
                col("split").in_(SPLITS),
                sa.func.jsonb_typeof(col("label")) == "object",
                sa.or_(col("base_sha").is_(None), col("base_sha").regexp_match(SHA)),
                sa.or_(col("unusable").is_(None), sa.func.char_length(col("unusable")).between(1, MAX_REASON_CHARS)),
            ),
            name="replay_tasks_check",
        ),
    )
    op.create_index("replay_tasks_split_idx", "replay_tasks", ["project_id", "split", "id"])


def _create_replays() -> None:
    op.create_table(
        "curator_replays",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("change_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("replay_key", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("result_a", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_b", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["change_id"], ["curator_changes.id"], ondelete="CASCADE", name="curator_replays_change_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_replays_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_replays_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["replay_tasks.id"], ondelete="CASCADE", name="curator_replays_task_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_replays_pkey"),
        sa.UniqueConstraint("change_id", "task_id", name="curator_replays_change_id_task_id_key"),
        sa.CheckConstraint(
            sa.and_(
                col("state").in_(STATES),
                col("attempts").between(0, MAX_ATTEMPTS),
                sa.or_(col("replay_key").is_(None), col("replay_key").regexp_match("^[0-9a-f]{64}$")),
                sa.or_(col("base_sha").is_(None), col("base_sha").regexp_match(SHA)),
                _result("result_a"),
                _result("result_b"),
                sa.or_(col("reason").is_(None), sa.func.char_length(col("reason")).between(1, MAX_REASON_CHARS)),
                (col("state") == "done") == sa.and_(col("result_a").is_not(None), col("result_b").is_not(None)),
                sa.or_(col("state") != "running", col("run_id").is_not(None)),
            ),
            name="curator_replays_check",
        ),
    )
    op.create_index("curator_replays_state_idx", "curator_replays", ["project_id", "state", "id"])


def upgrade() -> None:
    _kinds(KINDS)
    op.create_check_constraint(
        "runs_replay_schedule_check", "runs", sa.or_(col("kind") != "replay", col("schedule_id").is_not(None))
    )
    op.create_index(
        "runs_active_replay_key",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == "replay", col("state").in_(ACTIVE)),
    )
    _create_tasks()
    _create_replays()


def downgrade() -> None:
    op.drop_table("curator_replays")
    op.drop_table("replay_tasks")
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == "replay", runs.c.attempt == attempt))
    op.drop_index("runs_active_replay_key", table_name="runs")
    op.drop_constraint("runs_replay_schedule_check", "runs", type_="check")
    _kinds(KINDS_0052)
