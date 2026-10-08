"""Session digests, and the tool figures of every run that ended.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-08

``evo_agents.hub.digest`` and ``evo_agents.hub.server.tool_stats`` describe the design; the bounds below are copies of
their constants as of this revision, never imports, so a later change to them takes a new revision. Every statement
is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

session_digests holds one digest per Claude Code session of a project's directory, pushed by the Stop hook of the
evo-hub plugin: who pushed it, the project's label it carries, a few columns read without the body (cwd, messages,
model, when the session started and ended), and the digest itself, a JSON object. A session is one row per project;
the push of a later turn replaces it. A digest goes with its project; the daily job hub.prune_digests deletes the ones
not updated for 90 days.

run_tool_stats holds what the tool calls of a run came to, per tool: calls, the calls that failed and the time they
took. The hub writes it when the run ends, from the run's events, so it stays after hub.prune_run_events deleted them.

Going back drops both tables, which is the schema of 0012; nothing else refers to them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

MIN_MESSAGES = 6  # evo_agents.hub.digest.MIN_MESSAGES
MAX_SESSION_ID = 100  # evo_agents.hub.digest.MAX_SESSION_ID
MAX_TOOL_NAME = 200  # evo_agents.hub.digest.MAX_NAME

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _object(name: str):
    return sa.func.jsonb_typeof(col(name)) == "object"


def _length(name: str, most: int):
    return sa.func.char_length(col(name)).between(1, most)


def upgrade() -> None:
    op.create_table(
        "session_digests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.CheckConstraint(_length("session_id", MAX_SESSION_ID), name="session_digests_session_id_check"),
        sa.CheckConstraint(col("messages") >= MIN_MESSAGES, name="session_digests_messages_check"),
        sa.CheckConstraint(_object("label"), name="session_digests_label_check"),
        sa.CheckConstraint(_object("body"), name="session_digests_body_check"),
        sa.CheckConstraint(col("updated_at") >= col("created_at"), name="session_digests_updated_check"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="session_digests_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="session_digests_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="session_digests_pkey"),
        sa.UniqueConstraint("project_id", "session_id", name="session_digests_project_id_session_id_key"),
    )
    op.create_index("session_digests_project_idx", "session_digests", ["project_id", "updated_at"])
    op.create_index("session_digests_updated_idx", "session_digests", ["updated_at"])
    op.create_table(
        "run_tool_stats",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(_length("tool_name", MAX_TOOL_NAME), name="run_tool_stats_tool_name_check"),
        sa.CheckConstraint(col("calls") >= 1, name="run_tool_stats_calls_check"),
        sa.CheckConstraint(col("errors").between(0, col("calls")), name="run_tool_stats_errors_check"),
        sa.CheckConstraint(col("duration_ms") >= 0, name="run_tool_stats_duration_check"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_tool_stats_run_id_fkey"),
        sa.PrimaryKeyConstraint("run_id", "tool_name", name="run_tool_stats_pkey"),
    )


def downgrade() -> None:
    op.drop_table("run_tool_stats")
    op.drop_index("session_digests_updated_idx", table_name="session_digests")
    op.drop_index("session_digests_project_idx", table_name="session_digests")
    op.drop_table("session_digests")
