"""The words of a project's history for search, and the playbook of the Curator.

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-20

``evo_agents.hub.history_index`` describes the index and ``evo_agents.hub.server.history_search`` how it is read;
``evo_agents.hub.playbook`` describes the playbook and ``evo_agents.hub.server.playbook`` its routes. The values below
are copies of their constants as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables_sessions`` and
``evo_agents.hub.tables_curator`` describe the tables.

history_lines holds one row per entry of a line of a transcript (``source`` transcript: its transcript, session, file
and line) or of an event of a run (``source`` run: its run and the event's seq in ``line``): when it came, who (the
person, the agent, a call, a result, an error), its text within MAX_TEXT characters, and ``search``, the text's
tsvector of the configuration ``simple``, which its GIN index reads. A row goes with its transcript, its run or its
project. history_indexed holds how far each transcript (the parts read) and each run (the fingerprint it was read at)
is in the index, and a failed attempt's count, error and the moment the next may come.

curator_playbook holds the entries of a project's playbook: a lens or none, a lesson within MAX_PLAYBOOK_TEXT, its
evidence, its counters of helpful and harmful, the project's default label, the runs that added and last changed it,
and when and why it was retired. curator_playbook_deltas holds each delta: the entry, the run, the op, its evidence and
its note.

Going back drops the four tables, which is the schema that schema 0059 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

SOURCES = ("transcript", "run")  # evo_agents.hub.history_index.SOURCES
MAX_TEXT = 2000  # evo_agents.hub.history_index.TEXT_CHARS
MAX_ERROR = 2000  # evo_agents.hub.history_index.ERROR_CHARS
OPS = ("add", "helpful", "harmful", "retire")  # evo_agents.hub.playbook.OPS
MAX_PLAYBOOK_TEXT = 500  # evo_agents.hub.playbook.MAX_TEXT_CHARS
MAX_NOTE = 1000  # evo_agents.hub.playbook.MAX_NOTE_CHARS

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _count(name: str, kind=sa.Integer) -> sa.Column:
    return sa.Column(name, kind(), server_default="0", nullable=False)


def _create_lines() -> None:
    source = col("source")
    op.create_table(
        "history_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("transcript_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("line", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("search", postgresql.TSVECTOR(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="history_lines_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"], ["session_transcripts.id"], ondelete="CASCADE", name="history_lines_transcript_id_fkey"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="history_lines_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="history_lines_pkey"),
        sa.CheckConstraint(
            sa.and_(
                source.in_(SOURCES),
                (source == "transcript") == col("transcript_id").is_not(None),
                (source == "transcript") == col("session_id").is_not(None),
                (source == "run") == col("run_id").is_not(None),
                col("line") >= 1,
                sa.func.length(col("text")) <= MAX_TEXT,
                sa.func.length(col("role")) <= 200,
            ),
            name="history_lines_check",
        ),
    )
    op.create_index("history_lines_search_idx", "history_lines", ["search"], postgresql_using="gin")
    op.create_index("history_lines_project_idx", "history_lines", ["project_id", "source", "at"])
    op.create_index("history_lines_transcript_idx", "history_lines", ["transcript_id", "line"])
    op.create_index("history_lines_run_idx", "history_lines", ["run_id", "line"])


def _create_indexed() -> None:
    op.create_table(
        "history_indexed",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("transcript_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _count("parts"),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        _count("lines", sa.BigInteger),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        _count("attempts"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="history_indexed_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["session_transcripts.id"],
            ondelete="CASCADE",
            name="history_indexed_transcript_id_fkey",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="history_indexed_run_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="history_indexed_pkey"),
        sa.UniqueConstraint("transcript_id", name="history_indexed_transcript_id_key"),
        sa.UniqueConstraint("run_id", name="history_indexed_run_id_key"),
        sa.CheckConstraint(
            sa.and_(
                col("transcript_id").is_(None) != col("run_id").is_(None),
                col("parts") >= 0,
                col("lines") >= 0,
                col("attempts") >= 0,
                sa.func.coalesce(sa.func.length(col("error")), 0) <= MAX_ERROR,
            ),
            name="history_indexed_check",
        ),
    )


def _create_playbook() -> None:
    op.create_table(
        "curator_playbook",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("lens", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default="[]", nullable=False),
        _count("helpful"),
        _count("harmful"),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("created_run_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_run_id", sa.BigInteger(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_note", sa.Text(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_playbook_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["created_run_id"], ["runs.id"], ondelete="SET NULL", name="curator_playbook_created_run_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["updated_run_id"], ["runs.id"], ondelete="SET NULL", name="curator_playbook_updated_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_playbook_pkey"),
        sa.CheckConstraint(
            sa.and_(
                sa.func.length(col("text")).between(1, MAX_PLAYBOOK_TEXT),
                sa.func.jsonb_typeof(col("evidence")) == "array",
                col("helpful") >= 0,
                col("harmful") >= 0,
                col("lens").is_(None) | col("lens").regexp_match("^[a-z_]{1,40}$"),
                sa.func.coalesce(sa.func.length(col("retired_note")), 0) <= MAX_NOTE,
            ),
            name="curator_playbook_check",
        ),
    )
    op.create_index("curator_playbook_project_idx", "curator_playbook", ["project_id", "id"])
    op.create_table(
        "curator_playbook_deltas",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _stamp("created_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_playbook_deltas_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["curator_playbook.id"], ondelete="CASCADE", name="curator_playbook_deltas_entry_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], ondelete="SET NULL", name="curator_playbook_deltas_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="curator_playbook_deltas_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("op").in_(OPS),
                sa.func.jsonb_typeof(col("evidence")) == "array",
                sa.func.coalesce(sa.func.length(col("note")), 0) <= MAX_NOTE,
            ),
            name="curator_playbook_deltas_check",
        ),
    )
    op.create_index("curator_playbook_deltas_entry_idx", "curator_playbook_deltas", ["entry_id", "id"])


def upgrade() -> None:
    _create_lines()
    _create_indexed()
    _create_playbook()


def downgrade() -> None:
    op.drop_table("curator_playbook_deltas")
    op.drop_table("curator_playbook")
    op.drop_table("history_indexed")
    op.drop_table("history_lines")
