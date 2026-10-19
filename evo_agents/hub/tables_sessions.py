"""The tables of what the evo-hub plugin sends about the sessions of members' machines, which ``evo_agents.hub.tables``
adds to its metadata and exposes as its own attributes (``tables.session_digests``, ``tables.session_transcripts``,
``tables.transcript_parts``): queries name them there.

session_digests (0013) holds the digest of each session of a project's directory (``evo_agents.hub.server.digests``).
session_transcripts (0049) holds one row per transcript file of a session in a project, the session's own (``file`` main)
and each subagent's (``subagents/...``): who uploaded it first, from which machine and runtime, the label it carries,
how far into the file on the machine the hub holds it (``source_bytes``, ``lines``), its parts and their bytes, the
chain of their hashes, and ``deleted_at`` once its content was deleted. transcript_parts holds the parts in order,
each the blob of kind transcript that holds its lines (``evo_agents.hub.server.transcripts``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _count(name: str, kind=BigInteger) -> Column:
    return Column(name, kind, nullable=False, server_default="0")


def session_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(session_digests, session_transcripts, transcript_parts), added to ``metadata``."""
    digests = Table(
        "session_digests",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("session_id", Text, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("label", JSONB, nullable=False),
        Column("cwd", Text, nullable=False),
        Column("messages", Integer, nullable=False),
        Column("model", Text),
        Column("started_at", DateTime(timezone=True)),
        Column("ended_at", DateTime(timezone=True)),
        Column("body", JSONB, nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="session_digests_project_id_fkey"
        ),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="session_digests_user_id_fkey"),
        PrimaryKeyConstraint("id", name="session_digests_pkey"),
        UniqueConstraint("project_id", "session_id", name="session_digests_project_id_session_id_key"),
        Index("session_digests_project_idx", "project_id", "updated_at"),
        Index("session_digests_updated_idx", "updated_at"),
    )
    transcripts = Table(
        "session_transcripts",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("session_id", Text, nullable=False),
        Column("file", Text, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("runtime", Text, nullable=False),
        Column("machine", Text, nullable=False),
        Column("cwd", Text),
        Column("label", JSONB, nullable=False),
        _count("source_bytes"),
        _count("lines"),
        _count("parts", Integer),
        _count("size"),
        _count("raw_size"),
        Column("sha256", Text),
        Column("started_at", DateTime(timezone=True)),
        Column("ended_at", DateTime(timezone=True)),
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("deleted_at", DateTime(timezone=True)),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="session_transcripts_project_id_fkey"
        ),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT", name="session_transcripts_user_id_fkey"),
        PrimaryKeyConstraint("id", name="session_transcripts_pkey"),
        UniqueConstraint("project_id", "session_id", "file", name="session_transcripts_file_key"),
        Index("session_transcripts_project_idx", "project_id", "updated_at"),
        Index("session_transcripts_updated_idx", "updated_at"),
    )
    parts = Table(
        "transcript_parts",
        metadata,
        Column("transcript_id", BigInteger, nullable=False),
        Column("seq", Integer, nullable=False),
        Column("source_start", BigInteger, nullable=False),
        Column("source_end", BigInteger, nullable=False),
        Column("first_line", BigInteger, nullable=False),
        Column("lines", Integer, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("raw_size", BigInteger, nullable=False),
        _stamp("created_at"),
        ForeignKeyConstraint(
            ["transcript_id"],
            ["session_transcripts.id"],
            ondelete="CASCADE",
            name="transcript_parts_transcript_id_fkey",
        ),
        PrimaryKeyConstraint("transcript_id", "seq", name="transcript_parts_pkey"),
        UniqueConstraint("transcript_id", "source_start", name="transcript_parts_start_key"),
        Index("transcript_parts_sha256_idx", "sha256"),
    )
    return digests, transcripts, parts
