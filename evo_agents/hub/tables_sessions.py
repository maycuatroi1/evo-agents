"""The tables of what the evo-hub plugin sends about the sessions of members' machines, and of what the hub makes of
them, which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own attributes
(``tables.session_digests``, ``tables.session_transcripts``, ``tables.transcript_parts``, ``tables.session_facets``):
queries name them there.

session_digests (0013) holds the digest of each session of a project's directory (``evo_agents.hub.server.digests``).
session_transcripts (0049) holds one row per transcript file of a session in a project, the session's own (``file`` main)
and each subagent's (``subagents/...``): who uploaded it first, from which machine and runtime, the label it carries,
how far into the file on the machine the hub holds it (``source_bytes``, ``lines``), its parts and their bytes, the
chain of their hashes, and ``deleted_at`` once its content was deleted. transcript_parts holds the parts in order,
each the blob of kind transcript that holds its lines (``evo_agents.hub.server.transcripts``). session_facets (0051)
holds the deterministic facet of each session and of each run that ended (``evo_agents.hub.facet_job``), and since 0056
the facet a cheap model wrote of it (``llm``, ``evo_agents.hub.history``). history_runs (0056) holds one row per
history run of the night shift, and facet_clusters (0056) the groups of a project's facets
(``evo_agents.hub.server.history``). history_lines (0057) holds the words of the transcripts and run logs, one row per
entry of a line, for search, and history_indexed (0057) how far each transcript and run is in it
(``evo_agents.hub.history_index``).
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
    column,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _count(name: str, kind=BigInteger) -> Column:
    return Column(name, kind, nullable=False, server_default="0")


def session_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    """(session_digests, session_transcripts, transcript_parts, session_facets), added to ``metadata``."""
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
    return digests, transcripts, parts, facet_table(metadata)


def facet_table(metadata: MetaData) -> Table:
    """session_facets (0051), added to ``metadata``."""
    return Table(
        "session_facets",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("session_id", Text),
        Column("run_id", BigInteger),
        Column("source", Text),
        Column("fingerprint", Text),
        _count("version", Integer),
        Column("user_id", BigInteger),
        Column("runtime", Text),
        Column("label", JSONB),
        Column("at", DateTime(timezone=True)),
        Column("summary", JSONB, nullable=False, server_default="{}"),
        Column("body", JSONB),
        Column("analyzed_at", DateTime(timezone=True)),
        _count("attempts", Integer),
        Column("error", Text),
        Column("retry_at", DateTime(timezone=True)),
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("llm", JSONB),  # 0056: the facet a cheap model wrote, and how it went
        _count("llm_version", Integer),
        Column("llm_fingerprint", Text),
        Column("llm_at", DateTime(timezone=True)),
        Column("llm_run_id", BigInteger),
        _count("llm_attempts", Integer),
        Column("llm_error", Text),
        Column("llm_retry_at", DateTime(timezone=True)),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="session_facets_project_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="session_facets_run_id_fkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL", name="session_facets_user_id_fkey"),
        ForeignKeyConstraint(["llm_run_id"], ["runs.id"], ondelete="SET NULL", name="session_facets_llm_run_id_fkey"),
        PrimaryKeyConstraint("id", name="session_facets_pkey"),
        Index(
            "session_facets_session_key",
            "project_id",
            "session_id",
            unique=True,
            postgresql_where=column("kind") == "session",
        ),
        Index("session_facets_run_key", "run_id", unique=True, postgresql_where=column("kind") == "run"),
        Index("session_facets_project_idx", "project_id", "at"),
    )


def history_tables(metadata: MetaData, runs: Table, active) -> tuple[Table, Table]:
    """(history_runs, facet_clusters) of 0056, added to ``metadata``, and the index that keeps one history run of a
    project active at a time: ``active`` are the states it counts (``tables.RUN_ACTIVE``)."""
    Index(
        "runs_active_history_key",
        runs.c.project_id,
        unique=True,
        postgresql_where=(runs.c.kind == "history") & runs.c.state.in_(active),
    )
    history_runs = Table(
        "history_runs",
        metadata,
        Column("run_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("key_digest", Text),
        _count("analyzed", Integer),
        _count("failed", Integer),
        _count("stale", Integer),
        _count("named", Integer),
        _stamp("created_at"),
        _stamp("updated_at"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="history_runs_run_id_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="history_runs_project_id_fkey"),
        PrimaryKeyConstraint("run_id", name="history_runs_pkey"),
        Index("history_runs_project_idx", "project_id", "run_id"),
    )
    clusters = Table(
        "facet_clusters",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("key", Text, nullable=False),
        Column("name", Text),
        Column("description", Text),
        Column("merged_into", Text),
        _count("sessions", Integer),
        _count("runs", Integer),
        Column("member_ids", ARRAY(BigInteger), nullable=False, server_default="{}"),
        Column("examples", JSONB, nullable=False, server_default="[]"),
        Column("named_run_id", BigInteger),
        Column("named_at", DateTime(timezone=True)),
        Column("counted_at", DateTime(timezone=True)),
        _stamp("created_at"),
        _stamp("updated_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="facet_clusters_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["named_run_id"], ["runs.id"], ondelete="SET NULL", name="facet_clusters_named_run_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="facet_clusters_pkey"),
        UniqueConstraint("project_id", "key", name="facet_clusters_project_id_key_key"),
    )
    return history_runs, clusters


def search_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(history_lines, history_indexed) of 0057, added to ``metadata``."""
    lines = Table(
        "history_lines",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("source", Text, nullable=False),
        Column("transcript_id", BigInteger),
        Column("run_id", BigInteger),
        Column("session_id", Text),
        Column("file", Text),
        Column("line", BigInteger, nullable=False),
        Column("at", DateTime(timezone=True)),
        Column("role", Text, nullable=False),
        Column("text", Text, nullable=False),
        Column("search", TSVECTOR, nullable=False),  # to_tsvector('simple', text), written with the row
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="history_lines_project_id_fkey"),
        ForeignKeyConstraint(
            ["transcript_id"], ["session_transcripts.id"], ondelete="CASCADE", name="history_lines_transcript_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="history_lines_run_id_fkey"),
        PrimaryKeyConstraint("id", name="history_lines_pkey"),
        Index("history_lines_search_idx", "search", postgresql_using="gin"),
        Index("history_lines_project_idx", "project_id", "source", "at"),
        Index("history_lines_transcript_idx", "transcript_id", "line"),
        Index("history_lines_run_idx", "run_id", "line"),
    )
    indexed = Table(
        "history_indexed",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("transcript_id", BigInteger),
        Column("run_id", BigInteger),
        _count("parts", Integer),
        Column("fingerprint", Text),
        _count("lines"),
        Column("indexed_at", DateTime(timezone=True)),
        _count("attempts", Integer),
        Column("error", Text),
        Column("retry_at", DateTime(timezone=True)),
        _stamp("created_at"),
        _stamp("updated_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="history_indexed_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["transcript_id"], ["session_transcripts.id"], ondelete="CASCADE", name="history_indexed_transcript_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="history_indexed_run_id_fkey"),
        PrimaryKeyConstraint("id", name="history_indexed_pkey"),
        UniqueConstraint("transcript_id", name="history_indexed_transcript_id_key"),
        UniqueConstraint("run_id", name="history_indexed_run_id_key"),
    )
    return lines, indexed
