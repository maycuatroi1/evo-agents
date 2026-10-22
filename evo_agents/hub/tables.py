"""The hub's tables as SQLAlchemy Core metadata, which queries are written on and Alembic autogenerates the next
migration from (``migrations/env.py``); the modules ``tables_<area>`` imported below hold them by area, and
``tables.srs``, ``tables.tasks`` and ``tables.canvas`` are core plugins'. The migrations make the schema and this
module describes it; ``tests/hub/test_schema_metadata.py`` keeps the two equal: columns, types, nullability, server
defaults, identities, keys and indexes, named as the migrations name them (CHECK constraints and triggers live in the
migrations only). A table is an attribute of its name."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
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

from evo_agents.hub import (
    tables_accounts,
    tables_curator,
    tables_goals,
    tables_kg,
    tables_loop,
    tables_merge,
    tables_outputs,
    tables_plugin_notices,
    tables_plugins,
    tables_pm,
    tables_profiles,
    tables_release,
    tables_sessions,
    tables_settings,
    tables_signals,
    tables_triage,
    tables_workers,
)
from evo_agents.hub.tables_chats import chat_tables

metadata = MetaData()

# Run states the partial indexes of runs name (evo_agents/hub/migrations/versions/0010_plan_runs.py and 0014).
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
    # Alembic compares a generated column's expression as text, warns at Postgres's casts and cannot alter it anyway
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
    Column("run_id", BigInteger),  # the author run that wrote the revision, NULL for any other write (0021)
    ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT", name="plan_revisions_actor_id_fkey"),
    ForeignKeyConstraint(  # runs name a revision of their plan too: created after both tables
        ["run_id"], ["runs.id"], ondelete="SET NULL", name="plan_revisions_run_id_fkey", use_alter=True
    ),
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
Index("skills_global_key", func.lower(skills.c.name), unique=True, postgresql_where=skills.c.scope == "global")
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

# The audit (0001, 0007); the knowledge graph's tables are in tables_kg

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

# Workers and runs (0009, 0010, 0011, 0012, 0014, 0017)

workers, worker_projects, worker_pairings, hub_nightly = tables_workers.worker_tables(metadata)  # 0009 to 0065

runs = Table(
    "runs",
    metadata,
    _id(),
    Column("project_id", BigInteger, nullable=False),
    Column("plan_id", Text),  # NULL for a review run, on no plan (0014), and an author run of a new one (0020)
    Column("step_key", Text),
    Column("title", Text),
    Column("plan_revision", Integer),
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
    Column("schedule_id", BigInteger),
    Column("schedule_night", Date),
    Column("budget", JSONB),
    Column("request", Text),  # the member's request of an author run, NULL for any other kind (0020)
    _when("finish_requested_at"),  # the owner ended the chat of an author run a worker holds (0022)
    Column("failure_cause", Text),  # why the run failed, as its worker reported it (0024)
    Column("session_per_step", Boolean, nullable=False, server_default=false()),  # a session for each step (0025)
    # A local run, a Claude Code session on a member's machine (0033); NULL for any other kind. Its session_id is the
    # Claude Code session id, and its worker_id stays NULL.
    Column("machine", Text),
    Column("cwd", Text),
    Column("claude_email", Text),  # the Claude account that ran the session; 'unknown' when it could not be read
    Column("claude_profile", Text),  # the claude-profiles profile, when it could be read
    _when("last_activity_at"),
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
    ForeignKeyConstraint(["schedule_id"], ["schedules.id"], ondelete="RESTRICT", name="runs_schedule_id_fkey"),
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
    "runs_active_review_key",
    runs.c.project_id,
    unique=True,
    postgresql_where=(runs.c.kind == "review") & runs.c.state.in_(RUN_ACTIVE),
)
Index(  # a project has one judge run active at a time (0017)
    "runs_active_judge_key",
    runs.c.project_id,
    unique=True,
    postgresql_where=(runs.c.kind == "judge") & runs.c.state.in_(RUN_ACTIVE),
)
Index(  # a session registers once per project, however often it says so again (0033)
    "runs_local_session_key",
    runs.c.project_id,
    runs.c.session_id,
    unique=True,
    postgresql_where=runs.c.kind == "local",
)
Index(  # the reaper finds the local runs that went silent (0033)
    "runs_local_silent_idx",
    runs.c.last_activity_at,
    postgresql_where=(runs.c.kind == "local") & (runs.c.state != "ended"),
)
Index("runs_claim_idx", runs.c.project_id, runs.c.dispatched_by, runs.c.id, postgresql_where=runs.c.state == "queued")
Index("runs_lease_idx", runs.c.lease_expires_at, postgresql_where=runs.c.state.in_(RUN_HELD))
Index("runs_worker_idx", runs.c.worker_id, runs.c.id.desc(), postgresql_where=runs.c.worker_id.is_not(None))
Index("runs_project_idx", runs.c.project_id, runs.c.id.desc())
Index("runs_step_idx", runs.c.project_id, runs.c.plan_id, runs.c.step_key, runs.c.id.desc())
Index("runs_waiting_idx", runs.c.waiting_since, postgresql_where=runs.c.state == "waiting")
Index("runs_parked_idx", runs.c.parked_at, postgresql_where=runs.c.state == "parked")
Index("runs_schedule_idx", runs.c.schedule_id, runs.c.schedule_night, postgresql_where=runs.c.schedule_id.is_not(None))

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

# Decisions and notifications (0010, 0014)

decisions = Table(
    "decisions",
    metadata,
    _id(),
    Column("run_id", BigInteger, nullable=False),
    Column("project_id", BigInteger, nullable=False),
    Column("plan_id", Text),  # NULL for a decision an author run asked before it put a plan (schema 0029)
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
Index("run_inbox_pending_idx", run_inbox.c.run_id, run_inbox.c.id, postgresql_where=run_inbox.c.delivered_at.is_(None))

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
    Column("proposal_id", BigInteger),
    ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="CASCADE", name="notifications_decision_id_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="notifications_project_id_fkey"),
    ForeignKeyConstraint(["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="notifications_proposal_id_fkey"),
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
Index(
    "notifications_proposal_key",
    notifications.c.proposal_id,
    notifications.c.user_id,
    unique=True,
    postgresql_where=notifications.c.proposal_id.is_not(None),
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
    Column("token_id", BigInteger),  # the web session that linked the channel, which it lives no longer than (0016)
    ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="CASCADE", name="notification_channels_token_id_fkey"),
    ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="notification_channels_user_id_fkey"),
    PrimaryKeyConstraint("id", name="notification_channels_pkey"),
    UniqueConstraint("user_id", "kind", name="notification_channels_user_id_kind_key"),
    Index("notification_channels_token_idx", "token_id"),
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
    Column("external_id", Text),
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
Index(  # a reply to a message a channel sent finds its notification (0015)
    "notification_deliveries_external_idx",
    notification_deliveries.c.channel_id,
    notification_deliveries.c.external_id,
    postgresql_where=notification_deliveries.c.external_id.is_not(None),
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

# The night shift (0012)

charters = Table(
    "charters",
    metadata,
    Column("project_id", BigInteger, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("body", JSONB, nullable=False),
    Column("worker_id", BigInteger, nullable=False),
    Column("created_by", BigInteger, nullable=False),
    _stamp("created_at"),
    ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="charters_created_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="charters_project_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="charters_worker_id_fkey"),
    PrimaryKeyConstraint("project_id", "revision", name="charters_pkey"),
)

schedules = Table(
    "schedules",
    metadata,
    _id(),
    Column("project_id", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("owner_id", BigInteger, nullable=False),
    Column("worker_id", BigInteger, nullable=False),
    _stamp("created_at"),
    _stamp("updated_at"),
    _when("paused_at"),
    Column("paused_by", BigInteger),
    Column("pause_reason", Text),  # why the hub paused it, its circuit breaker; NULL when a member did (0018)
    ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="schedules_owner_id_fkey"),
    ForeignKeyConstraint(["paused_by"], ["users.id"], ondelete="RESTRICT", name="schedules_paused_by_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="schedules_project_id_fkey"),
    ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="schedules_worker_id_fkey"),
    PrimaryKeyConstraint("id", name="schedules_pkey"),
    UniqueConstraint("project_id", "kind", name="schedules_project_id_kind_key"),
)

# The tool figures of runs (0013, and the skills of 0087); the session digests are in tables_sessions

run_tool_stats = Table(
    "run_tool_stats",
    metadata,
    Column("run_id", BigInteger, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("calls", Integer, nullable=False),
    Column("errors", Integer, nullable=False),
    Column("duration_ms", BigInteger, nullable=False),
    Column("skills", JSONB(none_as_null=True)),  # a Python None is SQL NULL, not JSON null
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_tool_stats_run_id_fkey"),
    PrimaryKeyConstraint("run_id", "tool_name", name="run_tool_stats_pkey"),
)

# The review runs of the night shift: the night's figures, and what the Reviewer finds and proposes (0014)

curator_figures = Table(
    "curator_figures",
    metadata,
    _id(),
    Column("project_id", BigInteger, nullable=False),
    Column("schedule_id", BigInteger, nullable=False),
    Column("night", Date, nullable=False),
    Column("since", DateTime(timezone=True), nullable=False),
    Column("until", DateTime(timezone=True), nullable=False),
    Column("figures", JSONB, nullable=False),
    Column("run_id", BigInteger),
    _stamp("created_at"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_figures_project_id_fkey"),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT", name="curator_figures_run_id_fkey"),
    ForeignKeyConstraint(
        ["schedule_id"], ["schedules.id"], ondelete="CASCADE", name="curator_figures_schedule_id_fkey"
    ),
    PrimaryKeyConstraint("id", name="curator_figures_pkey"),
    UniqueConstraint("project_id", "night", name="curator_figures_project_id_night_key"),
)

findings = Table(
    "findings",
    metadata,
    _id(),
    Column("project_id", BigInteger, nullable=False),
    Column("run_id", BigInteger, nullable=False),
    Column("lens", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text),
    Column("evidence", JSONB, nullable=False),
    Column("label", JSONB, nullable=False),
    _stamp("created_at"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="findings_project_id_fkey"),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="findings_run_id_fkey"),
    PrimaryKeyConstraint("id", name="findings_pkey"),
    Index("findings_run_idx", "run_id", "id"),
)
Index("findings_project_idx", findings.c.project_id, findings.c.id.desc())

proposals = Table(
    "proposals",
    metadata,
    _id(),
    Column("project_id", BigInteger, nullable=False),
    Column("run_id", BigInteger, nullable=False),
    Column("lens", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("paths", JSONB, nullable=False),
    Column("impacted", JSONB),
    Column("tier", SmallInteger, nullable=False),
    Column("tier_reasons", JSONB, nullable=False),
    Column("finding_ids", ARRAY(BigInteger), nullable=False, server_default="{}"),
    Column("evidence", JSONB, nullable=False),
    Column("evidence_count", Integer, nullable=False),
    Column("draft", JSONB, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("state", Text, nullable=False, server_default="open"),
    Column("duplicate_of", BigInteger),
    Column("answered_by", BigInteger),
    _when("answered_at"),
    Column("note", Text),
    _when("deferred_until"),
    _when("inbox_at"),
    Column("label", JSONB, nullable=False),
    _stamp("created_at"),
    Column("revert_of", BigInteger),  # the proposal whose merged change the hub proposes to revert (0018)
    ForeignKeyConstraint(["answered_by"], ["users.id"], ondelete="RESTRICT", name="proposals_answered_by_fkey"),
    ForeignKeyConstraint(["duplicate_of"], ["proposals.id"], ondelete="RESTRICT", name="proposals_duplicate_of_fkey"),
    ForeignKeyConstraint(["revert_of"], ["proposals.id"], ondelete="SET NULL", name="proposals_revert_of_fkey"),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="proposals_project_id_fkey"),
    ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="proposals_run_id_fkey"),
    PrimaryKeyConstraint("id", name="proposals_pkey"),
    Index("proposals_run_idx", "run_id", "id"),
)
Index("proposals_project_idx", proposals.c.project_id, proposals.c.id.desc())
Index(
    "proposals_rejected_idx",
    proposals.c.project_id,
    proposals.c.fingerprint,
    proposals.c.answered_at.desc(),
    postgresql_where=proposals.c.state == "rejected",
)
Index("proposals_deferred_idx", proposals.c.deferred_until, postgresql_where=proposals.c.state == "deferred")
Index(
    "proposals_inbox_idx",
    proposals.c.project_id,
    proposals.c.inbox_at,
    postgresql_where=proposals.c.inbox_at.is_not(None),
)

curator_briefs = tables_curator.briefs_table(metadata)  # the Curator's morning briefs (0015)

channel_link_codes = Table(  # the one-time codes that link a member's channel: telegram_links until 0063
    "channel_link_codes",
    metadata,
    _id(),
    Column("user_id", BigInteger, nullable=False),
    Column("code_hash", Text, nullable=False),
    _stamp("created_at"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    _when("used_at"),
    Column("token_id", BigInteger),  # the web session that made the code (0016)
    Column("kind", Text, nullable=False),  # the kind of members' channel it links (0063)
    ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="CASCADE", name="channel_link_codes_token_id_fkey"),
    ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="channel_link_codes_user_id_fkey"),
    PrimaryKeyConstraint("id", name="channel_link_codes_pkey"),
    UniqueConstraint("code_hash", name="channel_link_codes_code_hash_key"),
    Index("channel_link_codes_user_idx", "user_id"),
)

accounts, account_policies, run_accounts, user_emails = tables_accounts.account_tables(metadata)  # 0027, 0066, 0068
agent_profiles, agent_profile_versions = tables_profiles.profile_tables(metadata, runs, credential_leases)  # 0030
chats, chat_members, chat_messages, chat_mentions, chat_reactions, chat_events = chat_tables(metadata, runs)  # 0031
plugins, plugin_versions, project_plugins, plugin_configs, run_plugins = tables_plugins.plugin_tables(metadata, runs)
plugin_tokens, plugin_backends, plugin_deliveries, project_agent_types = tables_plugins.backend_tables(metadata, runs)
hub_settings, builder_proposals, autonomy_policies = tables_settings.settings_tables(metadata)  # 0037, 0043, 0079
triage_chats, run_triage = tables_triage.triage_tables(metadata, projects, runs)  # 0044
run_outputs, run_output_links = tables_outputs.output_tables(metadata, projects)  # the outputs of runs (0089)
notification_mutes = tables_outputs.mute_table(metadata)  # the notices members turned off (0090)
srs, tasks, canvas = tables_plugins.core_plugin_tables(metadata)  # srs (0046), tasks (0047), canvas (0073)
signal_keys, signals, signal_ingest_keys, hub_events = tables_signals.signal_tables(metadata)  # 0074, 0077
goals, goal_revisions = tables_goals.goal_tables(metadata)  # the goals of a project (0091)
loop_states, loop_items, loop_item_events = tables_loop.loop_tables(metadata)  # the Loop of a project (0093)
session_digests, session_transcripts, transcript_parts, session_facets = tables_sessions.session_tables(metadata)
history_runs, facet_clusters = tables_sessions.history_tables(metadata, runs, RUN_ACTIVE)  # 0059
history_lines, history_indexed = tables_sessions.search_tables(metadata)  # the words of the history (0060)
curator_changes, curator_repo_checks, curator_ledger, replay_tasks, curator_replays = tables_curator.tables(
    metadata, runs, RUN_ACTIVE
)  # the Curator's changes, ledger and replays (0017, 0018, 0019, 0056), and runs_active_replay_key
curator_playbook, curator_playbook_deltas = tables_curator.playbook_tables(metadata)  # 0060
merge_queue, merge_events, merge_samples, merge_breakers = tables_merge.merge_tables(metadata)  # 0081-0084
pm_sends = tables_pm.pm_tables(metadata)  # 0086
plugin_notice_keys = tables_plugin_notices.plugin_notice_tables(metadata)  # 0097
kg_ingests, kg_configs, kg_pending_runs, kg_builds, kg_source_files, kg_health = tables_kg.kg_tables(metadata)
release_schedules, release_cycles, release_runs, release_tags, release_checks = tables_release.release_tables(
    metadata, runs, RUN_ACTIVE
)
