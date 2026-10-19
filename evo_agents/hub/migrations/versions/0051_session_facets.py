"""The deterministic facets of sessions and runs: what each transcript, digest or run log says, counted without a model.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-19

``evo_agents.hub.facet_job`` describes the design and ``evo_agents.hub.facets`` the body; the values below are copies
of their constants as of this revision, never imports, so a later change to them takes a new revision. Every
statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables_sessions`` describes the
table.

session_facets holds one row per session of a project (``kind`` session, keyed by its session id, made from its
transcripts or, while the hub holds none, its digest) and one per run that ended (``kind`` run, keyed by the run,
made from its log or its events; ``session_id`` is the run's session, a local run's Claude session). A row says what
its facet was made from (``source``, and ``fingerprint``: the content as it was then), the version of the facet
(``version``, 0 while none was made), the member, the runtime, the label of the session, the moment it counts at
(``at``), its numbers (``summary``) and the facet itself (``body``, NULL while none was made). A failed attempt keeps
the facet made before, counts ``attempts`` and sets ``error`` and ``retry_at``. A session's row has no foreign key to
its transcripts: deleting their content leaves the facet, which the hub keeps without a limit; a run's row goes with
its run, and every row with its project.

Going back drops the table, which is the schema 0050 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

KINDS = ("session", "run")  # evo_agents.hub.facets.KINDS
SOURCES = ("transcript", "digest", "run_log", "events")  # evo_agents.hub.facets.SOURCES
MAX_SESSION_ID = 100  # evo_agents.hub.digest.MAX_SESSION_ID
MAX_ERROR = 2000  # evo_agents.hub.facet_job.ERROR_CHARS

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    kind, source = col("kind"), col("source")
    op.create_table(
        "session_facets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=True),
        sa.Column("label", postgresql.JSONB(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.CheckConstraint(kind.in_(KINDS), name="session_facets_kind_check"),
        sa.CheckConstraint(
            sa.or_(
                sa.and_(kind == "session", col("session_id").is_not(None), col("run_id").is_(None)),
                sa.and_(kind == "run", col("run_id").is_not(None)),
            ),
            name="session_facets_key_check",
        ),
        sa.CheckConstraint(sa.func.length(col("session_id")) <= MAX_SESSION_ID, name="session_facets_session_check"),
        sa.CheckConstraint(sa.or_(source.is_(None), source.in_(SOURCES)), name="session_facets_source_check"),
        sa.CheckConstraint(sa.and_(col("version") >= 0, col("attempts") >= 0), name="session_facets_counts_check"),
        sa.CheckConstraint(
            sa.or_(col("body").is_(None), sa.func.jsonb_typeof(col("body")) == "object"),
            name="session_facets_body_check",
        ),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("summary")) == "object", name="session_facets_summary_check"),
        sa.CheckConstraint(sa.func.length(col("error")) <= MAX_ERROR, name="session_facets_error_check"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="session_facets_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="session_facets_run_id_fkey"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL", name="session_facets_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="session_facets_pkey"),
    )
    op.create_index(
        "session_facets_session_key",
        "session_facets",
        ["project_id", "session_id"],
        unique=True,
        postgresql_where=kind == "session",
    )
    op.create_index("session_facets_run_key", "session_facets", ["run_id"], unique=True, postgresql_where=kind == "run")
    op.create_index("session_facets_project_idx", "session_facets", ["project_id", "at"])


def downgrade() -> None:
    op.drop_index("session_facets_project_idx", table_name="session_facets")
    op.drop_index("session_facets_run_key", table_name="session_facets")
    op.drop_index("session_facets_session_key", table_name="session_facets")
    op.drop_table("session_facets")
