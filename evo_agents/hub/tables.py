"""The hub's 33 tables as SQLAlchemy Core metadata: what the queries are written on, and what Alembic autogenerates
the next migration from (``evo_agents/hub/migrations/env.py``).

The migrations make the schema; this module describes it, and ``tests/hub/test_schema_metadata.py`` keeps the two
equal (Alembic's ``compare_metadata`` on a database migrated to head finds no difference). Columns, types,
nullability, server defaults, identities, the generated column, keys, foreign keys and indexes are here, with the
names the migrations gave them. CHECK constraints and triggers live in the migrations only: Alembic does not compare
them, and a query has no use for them. procrastinate's tables belong to procrastinate and are not here.

Each table is a module attribute named as the table. Import the module (``from evo_agents.hub import tables``) rather
than the names, which would shadow modules such as ``secrets`` or ``evo_agents.hub.runs``.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    column,
    false,
    func,
    literal,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

metadata = MetaData()

# Run states the partial indexes of runs name (evo_agents/hub/migrations/versions/0010_plan_runs.py).
RUN_ACTIVE = ("queued", "leased", "running", "interactive", "verifying", "waiting", "review", "parked")
RUN_HELD = ("leased", "running", "interactive", "verifying", "waiting")


def _stamp(name: str) -> Column:
    """A timestamptz column that defaults to now() and is never NULL."""
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    """A timestamptz column that is NULL until it happens."""
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    """``id bigint GENERATED ALWAYS AS IDENTITY``."""
    return Column("id", BigInteger, Identity(always=True), nullable=False)


# Accounts and access (0001)

users = Table(
    "users",
    metadata,
    _id(),
    Column("login", Text, nullable=False),
    Column("github_id", BigInteger),
    _stamp("created_at"),
    _when("last_seen_at"),
    PrimaryKeyConstraint("id", name="users_pkey"),
    UniqueConstraint("github_id", name="users_github_id_key"),
)
Index("users_login_key", func.lower(users.c.login), unique=True)  # GitHub logins are case-insensitive

tokens = Table(
    "tokens",
    metadata,
    _id(),
    Column("user_id", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("token_hash", Text, nullable=False),
    Column("host", Text),
    _stamp("created_at"),
    _when("last_used_at"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    _when("revoked_at"),
    ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="tokens_user_id_fkey"),
    PrimaryKeyConstraint("id", name="tokens_pkey"),
    UniqueConstraint("token_hash", name="tokens_token_hash_key"),
    Index("tokens_user_id_idx", "user_id"),
)

projects = Table(
    "projects",
    metadata,
    _id(),
    Column("name", Text, nullable=False),
    Column("levels", ARRAY(Text), nullable=False),
    Column("locations", ARRAY(Text), nullable=False),
    Column("default_label", JSONB, nullable=False),
    Column("created_by", BigInteger, nullable=False),
    _stamp("created_at"),
    _stamp("updated_at"),
    Column("cluster", Text),
    Column("workspace", Text),
    Column("harness_path", Text),
    ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="projects_created_by_fkey"),
    PrimaryKeyConstraint("id", name="projects_pkey"),
    UniqueConstraint("name", name="projects_name_key"),
)

project_repos = Table(
    "project_repos",
    metadata,
    Column("project_id", BigInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("origin", Text),
    Column("default_branch", Text),
    Column("path", Text),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="project_repos_project_id_fkey"),
    PrimaryKeyConstraint("project_id", "name", name="project_repos_pkey"),
)

project_sinks = Table(
    "project_sinks",
    metadata,
    Column("project_id", BigInteger, nullable=False),
    Column("sink_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("clearance", JSONB, nullable=False),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="project_sinks_project_id_fkey"),
    PrimaryKeyConstraint("project_id", "sink_id", name="project_sinks_pkey"),
)
Index(
    "project_sinks_one_hub_key",
    project_sinks.c.project_id,
    unique=True,
    postgresql_where=project_sinks.c.kind == "hub",
)

grants = Table(
    "grants",
    metadata,
    Column("user_id", BigInteger, nullable=False),
    Column("project_id", BigInteger, nullable=False),
    Column("role", Text, nullable=False),
    Column("max_level", Text, nullable=False),
    Column("granted_by", BigInteger, nullable=False),
    _stamp("granted_at"),
    ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="RESTRICT", name="grants_granted_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="grants_project_id_fkey"),
    ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="grants_user_id_fkey"),
    PrimaryKeyConstraint("user_id", "project_id", name="grants_pkey"),
    Index("grants_project_id_idx", "project_id"),
)

# Memories, plans and skills (0001, 0003, 0005)

memories = Table(
    "memories",
    metadata,
    _id(),
    Column("scope", Text, nullable=False),
    Column("project_id", BigInteger),
    Column("location", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("label", JSONB, nullable=False),
    Column("body", Text, nullable=False),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("deleted", Boolean, nullable=False, server_default=false()),
    _stamp("created_at"),
    _stamp("updated_at"),
    Column("updated_by", BigInteger, nullable=False),
    # Alembic compares a generated column's expression as text and warns at the casts Postgres adds to it
    # ('simple'::regconfig); it cannot alter one anyway.
    Column(
        "search",
        TSVECTOR,
        Computed(
            func.to_tsvector(literal("simple", Text), column("name", Text) + " " + column("body", Text)),
            persisted=True,
        ),
    ),
    ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="memories_owner_id_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="memories_project_id_fkey"),
    ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT", name="memories_updated_by_fkey"),
    PrimaryKeyConstraint("id", name="memories_pkey"),
    Index("memories_search_idx", "search", postgresql_using="gin"),
    Index("memories_updated_idx", "updated_at", "id"),
)
Index(
    "memories_shared_key",
    memories.c.project_id,
    memories.c.location,
    memories.c.name,
    unique=True,
    postgresql_where=(memories.c.scope == "project") & memories.c.type.in_(("project", "reference")),
)
Index(
    "memories_owned_key",
    memories.c.owner_id,
    memories.c.project_id,
    memories.c.location,
    memories.c.name,
    unique=True,
    postgresql_where=(memories.c.scope == "project") & memories.c.type.in_(("user", "feedback")),
)
Index(
    "memories_personal_key",
    memories.c.owner_id,
    memories.c.location,
    memories.c.name,
    unique=True,
    postgresql_where=memories.c.scope == "personal",
)

memory_revisions = Table(
    "memory_revisions",
    metadata,
    Column("memory_id", BigInteger, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("type", Text, nullable=False),
    Column("label", JSONB, nullable=False),
    Column("body", Text, nullable=False),
    Column("deleted", Boolean, nullable=False),
    Column("actor_id", BigInteger, nullable=False),
    _stamp("created_at"),
    ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="memory_revisions_actor_id_fkey"),
    ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE", name="memory_revisions_memory_id_fkey"),
    PrimaryKeyConstraint("memory_id", "revision", name="memory_revisions_pkey"),
)

plans = Table(
    "plans",
    metadata,
    Column("project_id", BigInteger, nullable=False),
    Column("plan_id", Text, nullable=False),
    Column("area", Text, nullable=False),
    Column("label", JSONB, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("digest", Text, nullable=False),
    _stamp("created_at"),
    _stamp("updated_at"),
    Column("updated_by", BigInteger, nullable=False),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="plans_project_id_fkey"),
    ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT", name="plans_updated_by_fkey"),
    PrimaryKeyConstraint("project_id", "plan_id", name="plans_pkey"),
)

plan_revisions = Table(
    "plan_revisions",
    metadata,
    Column("project_id", BigInteger, nullable=False),
    Column("plan_id", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("area", Text, nullable=False),
    Column("label", JSONB, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("digest", Text, nullable=False),
    Column("summary", Text, nullable=False, server_default=""),
    Column("actor_id", BigInteger, nullable=False),
    _stamp("created_at"),
    ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="plan_revisions_actor_id_fkey"),
    ForeignKeyConstraint(
        ["project_id", "plan_id"],
        ["plans.project_id", "plans.plan_id"],
        ondelete="CASCADE",
        name="plan_revisions_project_id_plan_id_fkey",
    ),
    PrimaryKeyConstraint("project_id", "plan_id", "revision", name="plan_revisions_pkey"),
)

skills = Table(
    "skills",
    metadata,
    _id(),
    Column("scope", Text, nullable=False),
    Column("project_id", BigInteger),
    Column("name", Text, nullable=False),
    Column("created_by", BigInteger, nullable=False),
    _stamp("created_at"),
    ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="skills_created_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="skills_project_id_fkey"),
    PrimaryKeyConstraint("id", name="skills_pkey"),
)
Index(
    "skills_global_key",
    func.lower(skills.c.name),
    unique=True,
    postgresql_where=skills.c.scope == "global",
)
Index(
    "skills_project_key",
    skills.c.project_id,
    func.lower(skills.c.name),
    unique=True,
    postgresql_where=skills.c.scope == "project",
)

skill_versions = Table(
    "skill_versions",
    metadata,
    Column("skill_id", BigInteger, nullable=False),
    Column("version", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("sha256", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("r2_key", Text, nullable=False),
    Column("source_repo", Text),
    Column("source_commit", Text),
    Column("published_by", BigInteger, nullable=False),
    _stamp("published_at"),
    ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT", name="skill_versions_published_by_fkey"),
    ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE", name="skill_versions_skill_id_fkey"),
    PrimaryKeyConstraint("skill_id", "version", name="skill_versions_pkey"),
)

# Knowledge graph ingests and audit (0001, 0007)

kg_ingests = Table(
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

audit = Table(
    "audit",
    metadata,
    _id(),
    _stamp("at"),
    Column("actor_id", BigInteger),
    Column("token_id", BigInteger),
    Column("action", Text, nullable=False),
    Column("target", Text, nullable=False, server_default=""),
    Column("project_id", BigInteger),
    ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="audit_actor_id_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="audit_project_id_fkey"),
    ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="RESTRICT", name="audit_token_id_fkey"),
    PrimaryKeyConstraint("id", name="audit_pkey"),
    Index("audit_action_idx", "action", "at"),
    Index("audit_actor_id_idx", "actor_id", "at"),
    Index("audit_at_idx", "at"),
)
Index(
    "audit_project_id_idx",
    audit.c.project_id,
    audit.c.at,
    postgresql_where=audit.c.project_id.is_not(None),
)

# Blobs (0004, 0008)

blobs = Table(
    "blobs",
    metadata,
    Column("project_id", BigInteger),
    Column("sha256", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("created_by", BigInteger),
    _stamp("verified_at"),
    ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="blobs_created_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="blobs_project_id_fkey"),
    UniqueConstraint("project_id", "sha256", name="blobs_holder_key", postgresql_nulls_not_distinct=True),
    Index("blobs_sha256_idx", "sha256"),
)

blob_uploads = Table(
    "blob_uploads",
    metadata,
    Column("upload_id", Uuid, nullable=False),
    Column("project_id", BigInteger),
    Column("sha256", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("created_by", BigInteger, nullable=False),
    _stamp("created_at"),
    ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE", name="blob_uploads_created_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="blob_uploads_project_id_fkey"),
    PrimaryKeyConstraint("upload_id", name="blob_uploads_pkey"),
    Index("blob_uploads_created_at_idx", "created_at"),
)

blob_deletions = Table(
    "blob_deletions",
    metadata,
    Column("sha256", Text, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    _stamp("requested_at"),
    _when("deleted_at"),
    PrimaryKeyConstraint("sha256", name="blob_deletions_pkey"),
)
Index(
    "blob_deletions_pending_idx",
    blob_deletions.c.requested_at,
    postgresql_where=blob_deletions.c.deleted_at.is_(None),
)

# Knowledge graph builds (0006, 0008)

kg_configs = Table(
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

kg_pending_runs = Table(
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
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_pending_runs_project_id_fkey"),
    ForeignKeyConstraint(["pushed_by"], ["users.id"], ondelete="CASCADE", name="kg_pending_runs_pushed_by_fkey"),
    PrimaryKeyConstraint("project_id", "run_id", name="kg_pending_runs_pkey"),
)

kg_builds = Table(
    "kg_builds",
    metadata,
    _id(),
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
    ForeignKeyConstraint(
        ["artifact_reused_from"], ["kg_builds.id"], ondelete="RESTRICT", name="kg_builds_artifact_reused_from_fkey"
    ),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="kg_builds_project_id_fkey"),
    ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT", name="kg_builds_requested_by_fkey"),
    PrimaryKeyConstraint("id", name="kg_builds_pkey"),
    UniqueConstraint("job_id", name="kg_builds_job_id_key"),
)
Index("kg_builds_project_idx", kg_builds.c.project_id, kg_builds.c.id.desc())
Index(
    "kg_builds_succeeded_idx",
    kg_builds.c.project_id,
    kg_builds.c.id.desc(),
    postgresql_where=kg_builds.c.status == "succeeded",
)
Index(
    "kg_builds_artifact_idx",
    kg_builds.c.project_id,
    kg_builds.c.id.desc(),
    postgresql_where=kg_builds.c.artifact_sha256.is_not(None),
)
Index(
    "kg_builds_artifact_sha256_idx",
    kg_builds.c.artifact_sha256,
    postgresql_where=kg_builds.c.artifact_sha256.is_not(None),
)

# Workers and runs (0009, 0010, 0011)

workers = Table(
    "workers",
    metadata,
    _id(),
    Column("owner_id", BigInteger, nullable=False),
    Column("token_id", BigInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("hostname", Text, nullable=False),
    Column("os", Text, nullable=False),
    Column("arch", Text, nullable=False),
    Column("agent_version", Text, nullable=False),
    Column("slots", SmallInteger, nullable=False, server_default="1"),
    Column("labels", ARRAY(Text), nullable=False, server_default="{}"),
    Column("runtimes", JSONB, nullable=False, server_default="{}"),
    Column("checkouts", JSONB, nullable=False, server_default="{}"),
    Column("free_slots", SmallInteger),
    Column("allow_web_terminal", Boolean, nullable=False, server_default=false()),
    _stamp("created_at"),
    _when("last_heartbeat_at"),
    _when("drained_at"),
    _when("revoked_at"),
    Column("dispatch_from", Text, nullable=False, server_default="any"),
    ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="workers_owner_id_fkey"),
    ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="RESTRICT", name="workers_token_id_fkey"),
    PrimaryKeyConstraint("id", name="workers_pkey"),
    UniqueConstraint("token_id", name="workers_token_id_key"),
)
Index(
    "workers_name_key",
    workers.c.owner_id,
    func.lower(workers.c.name),
    unique=True,
    postgresql_where=workers.c.revoked_at.is_(None),
)

worker_projects = Table(
    "worker_projects",
    metadata,
    Column("worker_id", BigInteger, nullable=False),
    Column("project_id", BigInteger, nullable=False),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="worker_projects_project_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="CASCADE", name="worker_projects_worker_id_fkey"),
    PrimaryKeyConstraint("worker_id", "project_id", name="worker_projects_pkey"),
)

worker_pairings = Table(
    "worker_pairings",
    metadata,
    _id(),
    Column("code_selector", Text, nullable=False),
    Column("code_hash", Text, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("projects", ARRAY(BigInteger), nullable=False),
    Column("slots", SmallInteger, nullable=False, server_default="1"),
    Column("labels", ARRAY(Text), nullable=False, server_default="{}"),
    Column("allow_web_terminal", Boolean, nullable=False, server_default=false()),
    Column("attempts", SmallInteger, nullable=False, server_default="0"),
    _stamp("created_at"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    _when("used_at"),
    Column("worker_id", BigInteger),
    ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE", name="worker_pairings_owner_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="CASCADE", name="worker_pairings_worker_id_fkey"),
    PrimaryKeyConstraint("id", name="worker_pairings_pkey"),
    UniqueConstraint("worker_id", name="worker_pairings_worker_id_key"),
)
Index(
    "worker_pairings_selector_key",
    worker_pairings.c.code_selector,
    unique=True,
    postgresql_where=worker_pairings.c.used_at.is_(None),
)
Index(
    "worker_pairings_owner_idx",
    worker_pairings.c.owner_id,
    postgresql_where=worker_pairings.c.used_at.is_(None),
)

runs = Table(
    "runs",
    metadata,
    _id(),
    Column("project_id", BigInteger, nullable=False),
    Column("plan_id", Text, nullable=False),
    Column("step_key", Text),
    Column("title", Text),
    Column("plan_revision", Integer, nullable=False),
    Column("dispatched_by", BigInteger, nullable=False),
    Column("pinned_worker_id", BigInteger),
    Column("worker_id", BigInteger),
    Column("requested_runtime", Text, nullable=False),
    Column("runtime", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("approval", Text, nullable=False),
    Column("timeout_s", Integer, nullable=False),
    Column("attempt", SmallInteger, nullable=False, server_default="1"),
    Column("max_attempts", SmallInteger, nullable=False, server_default="3"),
    Column("parent_run_id", BigInteger),
    Column("state", Text, nullable=False, server_default="queued"),
    _when("lease_expires_at"),
    Column("session_id", Text),
    Column("repo", Text),
    Column("branch", Text),
    Column("commit_sha", Text),
    Column("diffstat", JSONB),
    Column("verify", JSONB),
    Column("evidence", Text),
    Column("usage", JSONB),
    Column("error", Text),
    Column("log_sha256", Text),
    Column("diff_sha256", Text),
    Column("event_seq", Integer, nullable=False, server_default="0"),
    Column("events_acked", Integer, nullable=False, server_default="0"),
    _when("cancel_requested_at"),
    _when("takeover_requested_at"),
    _when("handback_requested_at"),
    _stamp("queued_at"),
    _when("leased_at"),
    _when("started_at"),
    _when("finished_at"),
    Column("kind", Text, nullable=False, server_default="step"),
    Column("repos", JSONB),
    Column("model", Text),
    Column("run_seconds", Integer, nullable=False, server_default="0"),
    _when("counted_at"),
    _when("waiting_since"),
    _when("parked_at"),
    Column("resume_of_run_id", BigInteger),
    Column("dispatched_via", Text),
    ForeignKeyConstraint(["dispatched_by"], ["users.id"], ondelete="RESTRICT", name="runs_dispatched_by_fkey"),
    ForeignKeyConstraint(["parent_run_id"], ["runs.id"], ondelete="RESTRICT", name="runs_parent_run_id_fkey"),
    ForeignKeyConstraint(["pinned_worker_id"], ["workers.id"], ondelete="RESTRICT", name="runs_pinned_worker_id_fkey"),
    ForeignKeyConstraint(
        ["project_id", "plan_id", "plan_revision"],
        ["plan_revisions.project_id", "plan_revisions.plan_id", "plan_revisions.revision"],
        ondelete="RESTRICT",
        name="runs_project_id_plan_id_plan_revision_fkey",
    ),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="runs_project_id_fkey"),
    ForeignKeyConstraint(["resume_of_run_id"], ["runs.id"], ondelete="RESTRICT", name="runs_resume_of_run_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="runs_worker_id_fkey"),
    PrimaryKeyConstraint("id", name="runs_pkey"),
)
Index(
    "runs_active_step_key",
    runs.c.project_id,
    runs.c.plan_id,
    runs.c.step_key,
    unique=True,
    postgresql_where=(runs.c.kind == "step") & runs.c.state.in_(RUN_ACTIVE),
)
Index(
    "runs_active_plan_key",
    runs.c.project_id,
    runs.c.plan_id,
    unique=True,
    postgresql_where=(runs.c.kind == "plan") & runs.c.state.in_(RUN_ACTIVE),
)
Index(
    "runs_claim_idx",
    runs.c.project_id,
    runs.c.dispatched_by,
    runs.c.id,
    postgresql_where=runs.c.state == "queued",
)
Index("runs_lease_idx", runs.c.lease_expires_at, postgresql_where=runs.c.state.in_(RUN_HELD))
Index("runs_worker_idx", runs.c.worker_id, runs.c.id.desc(), postgresql_where=runs.c.worker_id.is_not(None))
Index("runs_project_idx", runs.c.project_id, runs.c.id.desc())
Index("runs_step_idx", runs.c.project_id, runs.c.plan_id, runs.c.step_key, runs.c.id.desc())
Index("runs_waiting_idx", runs.c.waiting_since, postgresql_where=runs.c.state == "waiting")
Index("runs_parked_idx", runs.c.parked_at, postgresql_where=runs.c.state == "parked")

run_events = Table(
    "run_events",
    metadata,
    Column("run_id", BigInteger, nullable=False),
    Column("seq", Integer, nullable=False),
    _stamp("at"),
    Column("kind", Text, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("truncated", Boolean, nullable=False, server_default=false()),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_events_run_id_fkey"),
    PrimaryKeyConstraint("run_id", "seq", name="run_events_pkey"),
)

# Decisions and notifications (0010)

decisions = Table(
    "decisions",
    metadata,
    _id(),
    Column("run_id", BigInteger, nullable=False),
    Column("project_id", BigInteger, nullable=False),
    Column("plan_id", Text, nullable=False),
    Column("step_key", Text),
    Column("category", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("context", Text),
    Column("options", JSONB, nullable=False),
    Column("state", Text, nullable=False, server_default="open"),
    Column("answer_option", Text),
    Column("answer_text", Text),
    Column("answered_by", BigInteger),
    _stamp("asked_at"),
    _when("answered_at"),
    _when("delivered_at"),
    ForeignKeyConstraint(["answered_by"], ["users.id"], ondelete="RESTRICT", name="decisions_answered_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="decisions_project_id_fkey"),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="decisions_run_id_fkey"),
    PrimaryKeyConstraint("id", name="decisions_pkey"),
    Index("decisions_run_idx", "run_id", "id"),
)
Index("decisions_project_idx", decisions.c.project_id, decisions.c.id.desc())
Index("decisions_open_idx", decisions.c.run_id, postgresql_where=decisions.c.state == "open")

run_inbox = Table(
    "run_inbox",
    metadata,
    _id(),
    Column("run_id", BigInteger, nullable=False),
    Column("sent_by", BigInteger, nullable=False),
    Column("body", Text, nullable=False),
    _stamp("created_at"),
    _when("delivered_at"),
    Column("decision_id", BigInteger),
    ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="CASCADE", name="run_inbox_decision_id_fkey"),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_inbox_run_id_fkey"),
    ForeignKeyConstraint(["sent_by"], ["users.id"], ondelete="RESTRICT", name="run_inbox_sent_by_fkey"),
    PrimaryKeyConstraint("id", name="run_inbox_pkey"),
)
Index(
    "run_inbox_pending_idx",
    run_inbox.c.run_id,
    run_inbox.c.id,
    postgresql_where=run_inbox.c.delivered_at.is_(None),
)

notifications = Table(
    "notifications",
    metadata,
    _id(),
    Column("user_id", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("notice_kind", Text),
    Column("project_id", BigInteger),
    Column("run_id", BigInteger),
    Column("decision_id", BigInteger),
    Column("title", Text, nullable=False),
    Column("body", Text),
    Column("details", JSONB),
    Column("link", Text),
    _stamp("created_at"),
    _when("read_at"),
    ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="CASCADE", name="notifications_decision_id_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="notifications_project_id_fkey"),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="notifications_run_id_fkey"),
    ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="notifications_user_id_fkey"),
    PrimaryKeyConstraint("id", name="notifications_pkey"),
)
Index("notifications_user_idx", notifications.c.user_id, notifications.c.id.desc())
Index("notifications_unread_idx", notifications.c.user_id, postgresql_where=notifications.c.read_at.is_(None))
Index(
    "notifications_decision_key",
    notifications.c.decision_id,
    notifications.c.user_id,
    unique=True,
    postgresql_where=notifications.c.decision_id.is_not(None),
)

notification_channels = Table(
    "notification_channels",
    metadata,
    _id(),
    Column("user_id", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("config", JSONB, nullable=False, server_default="{}"),
    Column("enabled", Boolean, nullable=False, server_default=true()),
    _stamp("created_at"),
    ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="notification_channels_user_id_fkey"),
    PrimaryKeyConstraint("id", name="notification_channels_pkey"),
    UniqueConstraint("user_id", "kind", name="notification_channels_user_id_kind_key"),
)

notification_deliveries = Table(
    "notification_deliveries",
    metadata,
    _id(),
    Column("notification_id", BigInteger, nullable=False),
    Column("channel_id", BigInteger),
    Column("state", Text, nullable=False, server_default="pending"),
    Column("attempts", SmallInteger, nullable=False, server_default="0"),
    _stamp("next_at"),
    Column("last_error", Text),
    _stamp("created_at"),
    _when("delivered_at"),
    ForeignKeyConstraint(
        ["channel_id"],
        ["notification_channels.id"],
        ondelete="CASCADE",
        name="notification_deliveries_channel_id_fkey",
    ),
    ForeignKeyConstraint(
        ["notification_id"],
        ["notifications.id"],
        ondelete="CASCADE",
        name="notification_deliveries_notification_id_fkey",
    ),
    PrimaryKeyConstraint("id", name="notification_deliveries_pkey"),
    Index("notification_deliveries_channel_key", "notification_id", "channel_id", unique=True),
)
Index(
    "notification_deliveries_web_key",
    notification_deliveries.c.notification_id,
    unique=True,
    postgresql_where=notification_deliveries.c.channel_id.is_(None),
)
Index(
    "notification_deliveries_due_idx",
    notification_deliveries.c.next_at,
    postgresql_where=notification_deliveries.c.state == "pending",
)

# Credentials (0011)

secrets = Table(
    "secrets",
    metadata,
    _id(),
    Column("owner_id", BigInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("env_var", Text),
    Column("url_prefix", Text),
    Column("username", Text),
    Column("sealed", LargeBinary),
    Column("nonce", LargeBinary),
    Column("key_id", Text),
    _stamp("created_at"),
    _stamp("updated_at"),
    _when("expires_at"),
    _when("deleted_at"),
    ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="secrets_owner_id_fkey"),
    PrimaryKeyConstraint("id", name="secrets_pkey"),
)
Index(
    "secrets_name_key",
    secrets.c.owner_id,
    secrets.c.name,
    unique=True,
    postgresql_where=secrets.c.deleted_at.is_(None),
)

secret_bindings = Table(
    "secret_bindings",
    metadata,
    Column("secret_id", BigInteger, nullable=False),
    Column("project_id", BigInteger, nullable=False),
    Column("worker_id", BigInteger),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="secret_bindings_project_id_fkey"),
    ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="CASCADE", name="secret_bindings_secret_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="CASCADE", name="secret_bindings_worker_id_fkey"),
    Index("secret_bindings_project_idx", "project_id"),
)
Index(
    "secret_bindings_key",
    secret_bindings.c.secret_id,
    secret_bindings.c.project_id,
    func.coalesce(secret_bindings.c.worker_id, 0),
    unique=True,
)
Index(
    "secret_bindings_worker_idx",
    secret_bindings.c.worker_id,
    postgresql_where=secret_bindings.c.worker_id.is_not(None),
)

credential_leases = Table(
    "credential_leases",
    metadata,
    _id(),
    Column("run_id", BigInteger, nullable=False),
    Column("worker_id", BigInteger, nullable=False),
    Column("secret_id", BigInteger),
    Column("provider", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("external_id", Text),
    _stamp("issued_at"),
    _when("expires_at"),
    _when("revoked_at"),
    Column("sealed_value", LargeBinary),
    Column("nonce", LargeBinary),
    Column("key_id", Text),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="credential_leases_run_id_fkey"),
    ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="RESTRICT", name="credential_leases_secret_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="credential_leases_worker_id_fkey"),
    PrimaryKeyConstraint("id", name="credential_leases_pkey"),
    Index("credential_leases_run_idx", "run_id", "id"),
)
Index(
    "credential_leases_worker_idx",
    credential_leases.c.worker_id,
    postgresql_where=credential_leases.c.revoked_at.is_(None),
)
Index(
    "credential_leases_secret_idx",
    credential_leases.c.secret_id,
    postgresql_where=credential_leases.c.revoked_at.is_(None),
)
Index(
    "credential_leases_sealed_idx",
    credential_leases.c.expires_at,
    postgresql_where=credential_leases.c.sealed_value.is_not(None),
)
