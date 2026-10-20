"""The transcripts of Claude Code and Codex sessions: an index per transcript file, and the parts its content came in.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-19

``evo_agents.hub.server.transcripts`` describes the design; the bounds below are copies of the constants of
``evo_agents.hub.transcripts`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables_sessions``
describes the tables.

session_transcripts holds one row per transcript file of a session in a project: the session's own transcript
(``file`` main) and each of its subagents' (``subagents/...``), with the member who uploaded it first, the machine,
the runtime (claude-code or codex), the session's directory, the project's label it carries, how much of the file the
hub holds (``source_bytes`` of the file on the machine, ``lines``), the parts and their bytes, the chain of the parts'
hashes (``sha256``), when the session's lines start and end, and ``deleted_at`` once an admin or the retention deleted
its content: the row stays, without parts, so a backfill does not upload it again.

transcript_parts holds each part of a transcript in order: the bytes of the file on the machine it covers, its first
line and how many lines it has, and the blob (kind transcript) that holds them, gzip of the redacted lines. A part
starts where the one before it ended, so ``(transcript_id, source_start)`` is unique.

Going back drops both tables, which is the schema 0051 runs on; the blobs of kind transcript stay as rows of their
project until an admin deletes them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

MAX_SESSION_ID = 100  # evo_agents.hub.digest.MAX_SESSION_ID
FILE_KEY = r"^(main|subagents(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}){1,5})$"  # evo_agents.hub.transcripts.FILE_KEY
MAX_FILE_KEY = 512  # evo_agents.hub.transcripts.MAX_FILE_KEY
RUNTIMES = ("claude-code", "codex")  # evo_agents.hub.transcripts.RUNTIMES
MACHINE = r"^[A-Za-z0-9-]{1,40}$"  # evo_agents.hub.memory.host_tag
MAX_CWD = 4096  # evo_agents.hub.digest.MAX_CWD
SHA256 = r"^[0-9a-f]{64}$"

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _count(name: str, kind=sa.BigInteger) -> sa.Column:
    return sa.Column(name, kind(), server_default="0", nullable=False)


def upgrade() -> None:
    op.create_table(
        "session_transcripts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("file", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("runtime", sa.Text(), nullable=False),
        sa.Column("machine", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        _count("source_bytes"),
        _count("lines"),
        _count("parts", sa.Integer),
        _count("size"),
        _count("raw_size"),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            sa.func.char_length(col("session_id")).between(1, MAX_SESSION_ID), name="session_transcripts_session_check"
        ),
        sa.CheckConstraint(
            sa.and_(sa.func.char_length(col("file")) <= MAX_FILE_KEY, col("file").regexp_match(FILE_KEY)),
            name="session_transcripts_file_check",
        ),
        sa.CheckConstraint(col("runtime").in_(RUNTIMES), name="session_transcripts_runtime_check"),
        sa.CheckConstraint(col("machine").regexp_match(MACHINE), name="session_transcripts_machine_check"),
        sa.CheckConstraint(sa.func.char_length(col("cwd")) <= MAX_CWD, name="session_transcripts_cwd_check"),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("label")) == "object", name="session_transcripts_label_check"),
        sa.CheckConstraint(
            sa.and_(
                col("source_bytes") >= 0,
                col("lines") >= 0,
                col("parts") >= 0,
                col("size") >= 0,
                col("raw_size") >= 0,
            ),
            name="session_transcripts_counts_check",
        ),
        sa.CheckConstraint(col("sha256").regexp_match(SHA256), name="session_transcripts_sha256_check"),
        sa.CheckConstraint(col("updated_at") >= col("created_at"), name="session_transcripts_updated_check"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="session_transcripts_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", name="session_transcripts_user_id_fkey"
        ),
        sa.PrimaryKeyConstraint("id", name="session_transcripts_pkey"),
        sa.UniqueConstraint("project_id", "session_id", "file", name="session_transcripts_file_key"),
    )
    op.create_index("session_transcripts_project_idx", "session_transcripts", ["project_id", "updated_at"])
    op.create_index("session_transcripts_updated_idx", "session_transcripts", ["updated_at"])
    op.create_table(
        "transcript_parts",
        sa.Column("transcript_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("source_start", sa.BigInteger(), nullable=False),
        sa.Column("source_end", sa.BigInteger(), nullable=False),
        sa.Column("first_line", sa.BigInteger(), nullable=False),
        sa.Column("lines", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("raw_size", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(col("seq") >= 1, name="transcript_parts_seq_check"),
        sa.CheckConstraint(
            sa.and_(col("source_start") >= 0, col("source_end") > col("source_start")),
            name="transcript_parts_source_check",
        ),
        sa.CheckConstraint(sa.and_(col("first_line") >= 1, col("lines") >= 1), name="transcript_parts_lines_check"),
        sa.CheckConstraint(col("sha256").regexp_match(SHA256), name="transcript_parts_sha256_check"),
        sa.CheckConstraint(sa.and_(col("size") >= 1, col("raw_size") >= 0), name="transcript_parts_size_check"),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["session_transcripts.id"],
            ondelete="CASCADE",
            name="transcript_parts_transcript_id_fkey",
        ),
        sa.PrimaryKeyConstraint("transcript_id", "seq", name="transcript_parts_pkey"),
        sa.UniqueConstraint("transcript_id", "source_start", name="transcript_parts_start_key"),
    )
    op.create_index("transcript_parts_sha256_idx", "transcript_parts", ["sha256"])


def downgrade() -> None:
    op.drop_index("transcript_parts_sha256_idx", table_name="transcript_parts")
    op.drop_table("transcript_parts")
    op.drop_index("session_transcripts_updated_idx", table_name="session_transcripts")
    op.drop_index("session_transcripts_project_idx", table_name="session_transcripts")
    op.drop_table("session_transcripts")
