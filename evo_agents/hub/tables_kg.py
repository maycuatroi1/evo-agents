"""The tables of a project's knowledge graph on the hub, which ``evo_agents.hub.tables`` adds to its metadata and exposes
as its own attributes (``tables.kg_ingests``, ``tables.kg_configs``, ``tables.kg_pending_runs``, ``tables.kg_builds``,
``tables.kg_source_files``, ``tables.kg_health``, ``tables.kg_review_dismissals``): queries name them there.

kg_ingests (0001) holds the connector runs machines pushed, kg_configs (0006) the knowledge config the build reads,
kg_pending_runs (0006) the runs waiting for their blobs, and kg_builds (0006, 0008) each build with its artifact
(``evo_agents.hub.server.kg``, ``evo_agents.hub.kg_build``). kg_builds.coverage, kg_source_files and kg_health (0055)
say how healthy the graph is over time: what each build covers, the files each git source holds in the latest
build, and each measure of the graph against the default branches on the forge (``evo_agents.hub.server.kg_health``).
kg_review_dismissals (0141) holds the items of the review queue writers dismissed (``evo_agents.kg.review``).
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
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _ingest_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(kg_ingests, kg_configs, kg_pending_runs), added to ``metadata``."""
    ingests = Table(
        "kg_ingests",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("run_id", Uuid, nullable=False),
        Column("source", Text, nullable=False),
        Column("log_sha256", Text, nullable=False),
        Column("log_size", BigInteger, nullable=False),
        Column("pushed_by", BigInteger, nullable=False),
        _stamp("received_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="kg_ingests_project_id_fkey"),
        ForeignKeyConstraint(["pushed_by"], ["users.id"], ondelete="RESTRICT", name="kg_ingests_pushed_by_fkey"),
        PrimaryKeyConstraint("project_id", "run_id", name="kg_ingests_pkey"),
    )
    configs = Table(
        "kg_configs",
        metadata,
        Column("project_id", BigInteger, nullable=False, autoincrement=False),
        Column("digest", Text, nullable=False),
        Column("knowledge", JSONB, nullable=False),
        Column("ontology", JSONB),
        Column("updated_by", BigInteger, nullable=False),
        _stamp("updated_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="kg_configs_project_id_fkey"),
        ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT", name="kg_configs_updated_by_fkey"),
        PrimaryKeyConstraint("project_id", name="kg_configs_pkey"),
    )
    pending = Table(
        "kg_pending_runs",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("run_id", Uuid, nullable=False),
        Column("source", Text, nullable=False),
        Column("log_sha256", Text, nullable=False),
        Column("log_size", BigInteger, nullable=False),
        Column("blobs", ARRAY(Text), nullable=False),
        Column("pushed_by", BigInteger, nullable=False),
        _stamp("created_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_pending_runs_project_id_fkey"
        ),
        ForeignKeyConstraint(["pushed_by"], ["users.id"], ondelete="CASCADE", name="kg_pending_runs_pushed_by_fkey"),
        PrimaryKeyConstraint("project_id", "run_id", name="kg_pending_runs_pkey"),
    )
    return ingests, configs, pending


def _builds(metadata: MetaData) -> Table:
    builds = Table(
        "kg_builds",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("job_id", BigInteger),
        Column("status", Text, nullable=False),
        Column("requested_by", BigInteger),
        Column("config_digest", Text),
        Column("runs", Integer),
        Column("artifact_sha256", Text),
        Column("artifact_size", BigInteger),
        Column("content_hash", Text),
        Column("nodes", Integer),
        Column("edges", Integer),
        Column("error", Text),
        _stamp("queued_at"),
        _when("started_at"),
        _when("finished_at"),
        Column("artifact_reused_from", BigInteger),
        _when("artifact_pruned_at"),
        Column("coverage", JSONB),
        ForeignKeyConstraint(
            ["artifact_reused_from"], ["kg_builds.id"], ondelete="RESTRICT", name="kg_builds_artifact_reused_from_fkey"
        ),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="kg_builds_project_id_fkey"),
        ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT", name="kg_builds_requested_by_fkey"),
        PrimaryKeyConstraint("id", name="kg_builds_pkey"),
        UniqueConstraint("job_id", name="kg_builds_job_id_key"),
    )
    Index("kg_builds_project_idx", builds.c.project_id, builds.c.id.desc())
    Index(
        "kg_builds_succeeded_idx",
        builds.c.project_id,
        builds.c.id.desc(),
        postgresql_where=builds.c.status == "succeeded",
    )
    Index(
        "kg_builds_artifact_idx",
        builds.c.project_id,
        builds.c.id.desc(),
        postgresql_where=builds.c.artifact_sha256.is_not(None),
    )
    Index(
        "kg_builds_artifact_sha256_idx",
        builds.c.artifact_sha256,
        postgresql_where=builds.c.artifact_sha256.is_not(None),
    )
    return builds


def _health_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(kg_source_files, kg_health), added to ``metadata`` (0055)."""
    files = Table(
        "kg_source_files",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("source", Text, nullable=False),
        Column("build_id", BigInteger, nullable=False),
        Column("read_commit", Text),
        Column("files", JSONB, nullable=False),
        Column("file_count", Integer, nullable=False),
        Column("truncated", Boolean, nullable=False, server_default=false()),
        _stamp("updated_at"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_source_files_project_id_fkey"
        ),
        ForeignKeyConstraint(["build_id"], ["kg_builds.id"], ondelete="CASCADE", name="kg_source_files_build_id_fkey"),
        PrimaryKeyConstraint("project_id", "source", name="kg_source_files_pkey"),
    )
    health = Table(
        "kg_health",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        _stamp("measured_at"),
        Column("build_id", BigInteger),
        Column("body", JSONB, nullable=False),
        Column("error", Text),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_health_project_id_fkey"),
        ForeignKeyConstraint(["build_id"], ["kg_builds.id"], ondelete="SET NULL", name="kg_health_build_id_fkey"),
        PrimaryKeyConstraint("id", name="kg_health_pkey"),
        Index("kg_health_project_idx", "project_id", "measured_at"),
    )
    return files, health


def review_dismissals(metadata: MetaData) -> Table:
    """kg_review_dismissals (0141), added to ``metadata``: the items of a project's review queue a writer dismissed,
    by fingerprint (``evo_agents.hub.server.kg_review``)."""
    return Table(
        "kg_review_dismissals",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("fingerprint", Text, nullable=False),
        Column("rule", Text, nullable=False),
        Column("reason", Text, nullable=False),
        Column("dismissed_by", BigInteger, nullable=False),
        _stamp("dismissed_at"),
        Column("build_id", BigInteger),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_review_dismissals_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["dismissed_by"], ["users.id"], ondelete="RESTRICT", name="kg_review_dismissals_dismissed_by_fkey"
        ),
        ForeignKeyConstraint(
            ["build_id"], ["kg_builds.id"], ondelete="SET NULL", name="kg_review_dismissals_build_id_fkey"
        ),
        PrimaryKeyConstraint("project_id", "fingerprint", name="kg_review_dismissals_pkey"),
    )


def kg_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table, Table, Table]:
    """(kg_ingests, kg_configs, kg_pending_runs, kg_builds, kg_source_files, kg_health), added to ``metadata``."""
    ingests, configs, pending = _ingest_tables(metadata)
    builds = _builds(metadata)
    return (ingests, configs, pending, builds, *_health_tables(metadata))
