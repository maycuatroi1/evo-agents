"""The night's history run: a facet a cheap model writes of each session and run, and the clusters they fall into.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-20

``evo_agents.hub.history`` describes the design and ``evo_agents.hub.server.history`` the routes and the night shift's
part; the values below are copies of their constants and of ``evo_agents.hub.runs`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions, as ``evo_agents.hub.tables_sessions`` describes the tables.

session_facets gains the facet of the model of each row (``llm``, NULL while none was written): its version
(``llm_version``, 0 while none), the fingerprint of the content it was written from (``llm_fingerprint``), when and by
which history run (``llm_at``, ``llm_run_id``, set to NULL when the run goes), and a failed attempt's count, error and
the moment the next may come (``llm_attempts``, ``llm_error``, ``llm_retry_at``).

history_runs holds one row per history run: the SHA-256 of the key its claim handed the daemon, and how many facets
it wrote, how many items it gave none, how many it read of content that changed since, and how many groups it named.

facet_clusters holds the groups of a project's facets, one per kind of task and first failure mode (``key``): the
name and description the model gave it, the group it said it is the same as (``merged_into``), and as the hub last
counted it, its sessions and runs, the members whose they are and its latest sessions (``examples``).

runs.kind takes ``history``: a run of the Curator on no plan and no repo (its repos NULL), which only the night shift
queues (``runs_history_schedule_check``), one active at a time per project (``runs_active_history_key``).

Going back drops the two tables and the columns, deletes the history runs and puts back the checks of 0055, which is
the schema that schema 0055 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

KINDS_0055 = ("step", "plan", "review", "judge", "author", "chat", "local", "task", "replay")
KINDS = (*KINDS_0055, "history")  # evo_agents.hub.runs.SHOWN_KINDS
NO_PLAN_0055 = ("review", "chat", "task")
NO_PLAN = (*NO_PLAN_0055, "history")
ANY_REPOS_0055 = ("chat", "local", "task")  # the kinds whose repos may be NULL: a project may have no repo
ANY_REPOS = (*ANY_REPOS_0055, "history")
ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")
KEY = r"^[a-z]+/(FM-[1-3]\.[1-6]|none)$"  # evo_agents.hub.history.KEY
MAX_ERROR = 2000  # evo_agents.hub.history.MAX_ERROR_CHARS
MAX_NAME_CHARS = 80  # evo_agents.hub.history.MAX_NAME_CHARS
MAX_DESCRIPTION_CHARS = 500  # evo_agents.hub.history.MAX_DESCRIPTION_CHARS
LLM_COLUMNS = (
    "llm",
    "llm_version",
    "llm_fingerprint",
    "llm_at",
    "llm_run_id",
    "llm_attempts",
    "llm_error",
    "llm_retry_at",
)

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), server_default="0", nullable=False)


def _replace(name: str, rule) -> None:
    op.drop_constraint(name, "runs", type_="check")
    op.create_check_constraint(name, "runs", rule)


def _checks(kinds, no_plan, any_repos) -> None:
    kind = col("kind")
    _replace("runs_kind_check", kind.in_(kinds))
    _replace(
        "runs_kind_plan_check", sa.or_(kind.in_(("author", "local")), kind.in_(no_plan) == col("plan_id").is_(None))
    )
    _replace("runs_kind_repos_check", sa.or_(kind.in_(any_repos), (kind == "step") == col("repos").is_(None)))


def _facet_columns() -> None:
    op.add_column("session_facets", sa.Column("llm", postgresql.JSONB(), nullable=True))
    op.add_column("session_facets", _count("llm_version"))
    op.add_column("session_facets", sa.Column("llm_fingerprint", sa.Text(), nullable=True))
    op.add_column("session_facets", sa.Column("llm_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("session_facets", sa.Column("llm_run_id", sa.BigInteger(), nullable=True))
    op.add_column("session_facets", _count("llm_attempts"))
    op.add_column("session_facets", sa.Column("llm_error", sa.Text(), nullable=True))
    op.add_column("session_facets", sa.Column("llm_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "session_facets_llm_run_id_fkey", "session_facets", "runs", ["llm_run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint(
        "session_facets_llm_check",
        "session_facets",
        sa.and_(
            sa.or_(col("llm").is_(None), sa.func.jsonb_typeof(col("llm")) == "object"),
            col("llm_version") >= 0,
            col("llm_attempts") >= 0,
            sa.func.coalesce(sa.func.length(col("llm_error")), 0) <= MAX_ERROR,
        ),
    )


def _create_history_runs() -> None:
    op.create_table(
        "history_runs",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("key_digest", sa.Text(), nullable=True),
        _count("analyzed"),
        _count("failed"),
        _count("stale"),
        _count("named"),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="history_runs_run_id_fkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="history_runs_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("run_id", name="history_runs_pkey"),
        sa.CheckConstraint(
            sa.and_(
                sa.or_(col("key_digest").is_(None), col("key_digest").regexp_match("^[0-9a-f]{64}$")),
                col("analyzed") >= 0,
                col("failed") >= 0,
                col("stale") >= 0,
                col("named") >= 0,
            ),
            name="history_runs_check",
        ),
    )
    op.create_index("history_runs_project_idx", "history_runs", ["project_id", "run_id"])


def _create_clusters() -> None:
    op.create_table(
        "facet_clusters",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("merged_into", sa.Text(), nullable=True),
        _count("sessions"),
        _count("runs"),
        sa.Column("member_ids", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("examples", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("named_run_id", sa.BigInteger(), nullable=True),
        sa.Column("named_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="facet_clusters_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["named_run_id"], ["runs.id"], ondelete="SET NULL", name="facet_clusters_named_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="facet_clusters_pkey"),
        sa.UniqueConstraint("project_id", "key", name="facet_clusters_project_id_key_key"),
        sa.CheckConstraint(
            sa.and_(
                col("key").regexp_match(KEY),
                sa.or_(
                    col("name").is_(None),
                    sa.and_(
                        sa.func.char_length(col("name")).between(1, MAX_NAME_CHARS),
                        ~col("name").op("~")(r"[\x01-\x1f\x7f]"),
                    ),
                ),
                sa.or_(
                    col("description").is_(None),
                    sa.func.char_length(col("description")).between(1, MAX_DESCRIPTION_CHARS),
                ),
                sa.or_(
                    col("merged_into").is_(None),
                    sa.and_(col("merged_into").regexp_match(KEY), col("merged_into") != col("key")),
                ),
                col("sessions") >= 0,
                col("runs") >= 0,
                col("runs") <= col("sessions"),
                sa.func.jsonb_typeof(col("examples")) == "array",
            ),
            name="facet_clusters_check",
        ),
    )


def upgrade() -> None:
    _checks(KINDS, NO_PLAN, ANY_REPOS)
    op.create_check_constraint(
        "runs_history_schedule_check", "runs", sa.or_(col("kind") != "history", col("schedule_id").is_not(None))
    )
    op.create_index(
        "runs_active_history_key",
        "runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.and_(col("kind") == "history", col("state").in_(ACTIVE)),
    )
    _facet_columns()
    _create_history_runs()
    _create_clusters()


def downgrade() -> None:
    op.drop_table("facet_clusters")
    op.drop_table("history_runs")
    op.drop_constraint("session_facets_llm_check", "session_facets", type_="check")
    op.drop_constraint("session_facets_llm_run_id_fkey", "session_facets", type_="foreignkey")
    for name in LLM_COLUMNS:
        op.drop_column("session_facets", name)
    runs = sa.table("runs", col("kind"), col("attempt"))
    for attempt in (3, 2, 1):  # a retry names the attempt before it: delete the retries first
        op.execute(runs.delete().where(runs.c.kind == "history", runs.c.attempt == attempt))
    op.drop_index("runs_active_history_key", table_name="runs")
    op.drop_constraint("runs_history_schedule_check", "runs", type_="check")
    _checks(KINDS_0055, NO_PLAN_0055, ANY_REPOS_0055)
