"""The tables of a project's knowledge graph on the hub, which ``evo_agents.hub.tables`` adds to its metadata and exposes
as its own attributes (``tables.kg_ingests``, ``tables.kg_configs``, ``tables.kg_pending_runs``, ``tables.kg_builds``,
``tables.kg_source_files``, ``tables.kg_health``, ``tables.kg_review_dismissals``, ``tables.kg_decisions``,
``tables.kg_decision_events``): queries name them there.

kg_ingests (0001) holds the connector runs machines pushed, kg_configs (0006) the knowledge config the build reads,
kg_pending_runs (0006) the runs waiting for their blobs, and kg_builds (0006, 0008) each build with its artifact
(``evo_agents.hub.server.kg``, ``evo_agents.hub.kg_build``). kg_builds.coverage, kg_source_files and kg_health (0055)
say how healthy the graph is over time: what each build covers, the files each git source holds in the latest
build, and each measure of the graph against the default branches on the forge (``evo_agents.hub.server.kg_health``).
kg_review_dismissals (0141) holds the items of the review queue writers dismissed (``evo_agents.kg.review``).
kg_decisions and kg_decision_events (0142) hold the decisions writers took on the graph and the proposals agents made,
with each step of their history (``evo_agents.hub.kg_decisions``). kg_query_labels (0178) holds each kg_search of a
session or a run with what the agent opened after it, under the label of the session or the run
(``evo_agents.hub.kg_labels``), as ``tables.kg_query_labels``. kg_eval_sets (0179) holds each version of a project's
retrieval question set (``evo_agents.hub.server.kg_eval_sets``), as ``tables.kg_eval_sets``. kg_optimize_runs (0182)
holds each run of the KG optimize loop (``evo_agents.hub.server.kg_optimize``), as ``tables.kg_optimize_runs``.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Double,
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
    column,
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
        Column("dismissed_by", BigInteger),  # a member; NULL when a core plugin dismissed it (0174)
        _stamp("dismissed_at"),
        Column("build_id", BigInteger),
        Column("dismissed_by_plugin", Text),  # the id of the core plugin that dismissed it, else NULL (0174)
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


def decision_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(kg_decisions, kg_decision_events) (0142), added to ``metadata``: each decision of a project's graph, by its
    number in the project, and each step of its history, by the project's revision it made
    (``evo_agents.hub.kg_decisions``)."""
    decisions = Table(
        "kg_decisions",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("kind", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("src", Text, nullable=False),
        Column("rel", Text),
        Column("dst", Text),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_decisions_project_id_fkey"),
        PrimaryKeyConstraint("id", name="kg_decisions_pkey"),
        UniqueConstraint("project_id", "number", name="kg_decisions_project_id_number_key"),
    )
    events = Table(
        "kg_decision_events",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("rev", Integer, nullable=False),
        Column("decision_id", BigInteger, nullable=False),
        Column("action", Text, nullable=False),
        Column("actor_id", BigInteger),  # a member; NULL when a core plugin took the step (0174)
        _stamp("at"),
        Column("reason", Text, nullable=False),
        Column("build_id", BigInteger),
        Column("actor_plugin", Text),  # the id of the core plugin that took it, else NULL (0174)
        Column("origin", Text),  # the loop that took it, as kg-optimize, else NULL (0180)
        Column("run_id", BigInteger),  # that loop's run, when it named one (0180)
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_decision_events_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["decision_id"], ["kg_decisions.id"], ondelete="CASCADE", name="kg_decision_events_decision_id_fkey"
        ),
        ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="kg_decision_events_actor_id_fkey"),
        ForeignKeyConstraint(
            ["build_id"], ["kg_builds.id"], ondelete="SET NULL", name="kg_decision_events_build_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="kg_decision_events_run_id_fkey"),
        PrimaryKeyConstraint("project_id", "rev", name="kg_decision_events_pkey"),
        Index("kg_decision_events_decision_idx", "decision_id"),
    )
    return decisions, events


def query_labels(metadata: MetaData) -> Table:
    """kg_query_labels (0178), added to ``metadata``: each kg_search of a session or a run as its facet read it, with
    the label of the session or the run (``evo_agents.hub.kg_labels``)."""
    name = "kg_query_labels"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("session_id", Text),
        Column("run_id", BigInteger),
        Column("seq", Integer, nullable=False),
        Column("at", Text),  # where the result came: FILE:LINE of a transcript, the seq of a run's event
        _when("searched_at"),
        Column("query", Text, nullable=False),
        Column("results", JSONB, nullable=False),
        Column("clicks", JSONB, nullable=False),
        Column("grep_after", Boolean, nullable=False),
        Column("after_grep", JSONB, nullable=False),
        Column("label", JSONB, nullable=False),
        _stamp("created_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name=f"{name}_run_id_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        CheckConstraint(column("kind").in_(("session", "run")), name=f"{name}_kind_check"),
        CheckConstraint(
            "(kind = 'session' AND session_id IS NOT NULL AND run_id IS NULL) OR (kind = 'run' AND run_id IS NOT NULL)",
            name=f"{name}_owner_check",
        ),
        Index(f"{name}_session_idx", "project_id", "session_id"),
        Index(f"{name}_run_idx", "run_id"),
        Index(f"{name}_project_idx", "project_id", "searched_at"),
    )


def eval_sets(metadata: MetaData) -> Table:
    """kg_eval_sets (0179), added to ``metadata``: each version of a project's question set for ``kg eval``, its
    name, its questions as JSON objects (``evo_agents.kg.evaluate``), the digest of what they ask and answer, and who
    put it."""
    name = "kg_eval_sets"
    return Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("version", Integer, nullable=False),
        Column("name", Text, nullable=False),
        Column("questions", JSONB, nullable=False),
        Column("digest", Text, nullable=False),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL", name=f"{name}_created_by_fkey"),
        PrimaryKeyConstraint("project_id", "version", name=f"{name}_pkey"),
        CheckConstraint("version >= 1", name=f"{name}_version_check"),
    )


def semantic_settings(metadata: MetaData) -> Table:
    """kg_semantic (0181), added to ``metadata``: whether a project's graph is searched by meaning too
    (``evo_agents.kg.semantic``): ``off``; ``index``, its builds write the vectors but kg_search ranks by words; ``on``,
    kg_search fuses both. A project without a row is off. The model the vectors come from, who set it last, when."""
    name = "kg_semantic"
    return Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("mode", Text, nullable=False),
        Column("model", Text, nullable=False),
        Column("updated_by", BigInteger),
        _stamp("updated_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name=f"{name}_updated_by_fkey"),
        PrimaryKeyConstraint("project_id", name=f"{name}_pkey"),
        CheckConstraint("mode IN ('off', 'index', 'on')", name=f"{name}_mode_check"),
        CheckConstraint("char_length(model) BETWEEN 1 AND 200", name=f"{name}_model_check"),
    )


OPTIMIZE_ACTIVE = ("measuring", "running", "after")  # the states of a KG optimize run in flight
OPTIMIZE_STATES = (*OPTIMIZE_ACTIVE, "done", "reverted", "failed")


def optimize_runs(metadata: MetaData) -> Table:
    """kg_optimize_runs (0182), added to ``metadata``: each run of the KG optimize loop of a project
    (``evo_agents.hub.server.kg_optimize``), by night or asked for, its state, the review run of the agent it queued,
    the builds measured before and after, the weight of the asked column it left in effect, what it measured and
    decided (``body``), and its report. One run a project is in flight at a time, one a night."""
    name = "kg_optimize_runs"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("via", Text, nullable=False),
        Column("requested_by", BigInteger),
        Column("night", Date),
        Column("state", Text, nullable=False),
        Column("agent_run_id", BigInteger),
        Column("before_build_id", BigInteger),
        Column("after_build_id", BigInteger),
        Column("asked_weight", Double),
        Column("body", JSONB, nullable=False),
        Column("report", Text),
        Column("notification_id", BigInteger),
        Column("error", Text),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("ended_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL", name=f"{name}_requested_by_fkey"),
        ForeignKeyConstraint(["agent_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{name}_agent_run_id_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        CheckConstraint(column("via").in_(("night", "manual")), name=f"{name}_via_check"),
        CheckConstraint(column("state").in_(OPTIMIZE_STATES), name=f"{name}_state_check"),
        Index(f"{name}_project_idx", "project_id", "id"),
        Index(f"{name}_agent_run_idx", "agent_run_id"),
    )
    Index(f"{name}_active_key", table.c.project_id, unique=True, postgresql_where=table.c.state.in_(OPTIMIZE_ACTIVE))
    Index(f"{name}_night_key", table.c.project_id, table.c.night, unique=True, postgresql_where=table.c.via == "night")
    return table


def kg_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table, Table, Table]:
    """(kg_ingests, kg_configs, kg_pending_runs, kg_builds, kg_source_files, kg_health), added to ``metadata``."""
    ingests, configs, pending = _ingest_tables(metadata)
    builds = _builds(metadata)
    return (ingests, configs, pending, builds, *_health_tables(metadata))
