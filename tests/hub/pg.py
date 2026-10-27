"""Postgres for the hub tests.

EVO_HUB_TEST_DSN names a server and a superuser on it; without it every hub test module skips. Each test
gets a database of its own, owned by a role of its own with a random password, so the hub runs without
superuser rights, a re-run starts clean, nothing lands in the server's own databases, and a password that
reaches a log line is easy to find. Such a database is a copy of a template the pytest process migrated once
(``make_template``, ``clone_database``): a copy takes about a quarter of the time of migrating an empty database,
which some 900 tests did each; a test of migrating itself gets an empty one, which a test of one revision makes a
copy of the template at the nearest tenth revision below it (``template_below``). The tests run in parallel with
``pytest -n auto`` (pytest-xdist): each worker has templates of its own, and nothing else is shared but the server.
Templates are migrated one revision a transaction (``migrate_in_steps``), and a test migrating the whole chain in one
transaction waits for the others (``one_whole_chain``): such a transaction holds some 2,700 locks until it commits,
and the lock table of a server with the default settings (max_locks_per_transaction 64 for each of 100 connections)
fits two.
psycopg, SQLAlchemy and Alembic are imported inside the helpers: this module loads on a core install too.

The SQL kept here is the CREATE, ALTER and DROP of databases and roles and the REASSIGN OWNED of a copy, which have no
SQLAlchemy construct; the catalogs it reads and the functions it calls are Core statements on ``admin_engine()``.
"""

import functools
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote

import pytest

DSN = os.environ.get("EVO_HUB_TEST_DSN", "").strip()
SKIP_REASON = "EVO_HUB_TEST_DSN is not set: hub tests need Postgres"
READY_TIMEOUT = 30.0
SERVER_STACK = (
    "fastapi",
    "uvicorn",
    "psycopg",
    "psycopg_pool",
    "alembic",
    "sqlalchemy",
    "httpx",
    "boto3",
    "procrastinate",
    "moto",
    "cryptography",
)
# Tables of migration 0004: the blob store's and procrastinate's job queue
BLOB_TABLES = frozenset({"blobs", "blob_uploads"})
QUEUE_TABLES = frozenset(
    {"procrastinate_jobs", "procrastinate_events", "procrastinate_periodic_defers", "procrastinate_workers"}
)
# Tables of migration 0006: knowledge graphs on the hub
KG_TABLES = frozenset({"kg_configs", "kg_pending_runs", "kg_builds"})
# Tables of migration 0008: blobs being deleted, such as the artifacts of old graphs
RETENTION_TABLES = frozenset({"blob_deletions"})
# Tables of migration 0009: workers, their pairings and projects, and the runs with their events and inbox
RUN_TABLES = frozenset({"workers", "worker_projects", "worker_pairings", "runs", "run_events", "run_inbox"})
# Tables of migration 0010: the decisions of plan runs, and the notifications, channels and deliveries
NOTIFICATION_TABLES = frozenset({"decisions", "notifications", "notification_channels", "notification_deliveries"})
# Tables of migration 0011: the secrets members keep, where they are bound, and the leases runs get of them
CREDENTIAL_TABLES = frozenset({"secrets", "secret_bindings", "credential_leases"})
# Tables of migration 0012: the charters of projects at every revision, and the schedules they make
CURATOR_TABLES = frozenset({"charters", "schedules"})
# Tables of migration 0013: the digests of Claude Code sessions, and the tool figures of runs
DIGEST_TABLES = frozenset({"session_digests", "run_tool_stats"})
# Tables of migration 0014: the night's figures of a project, and what its review run finds and proposes
REVIEW_TABLES = frozenset({"curator_figures", "findings", "proposals"})
# Tables of migration 0015: the Curator's morning briefs, and the codes that link a Telegram chat
BRIEF_TABLES = frozenset({"curator_briefs", "channel_link_codes"})  # telegram_links until 0063
# migration 0017: the Curator's changes, and the repos whose ruleset the hub checked
CHANGE_TABLES = frozenset({"curator_changes", "curator_repo_checks"})
# migration 0018: the Curator's ledger
LEDGER_TABLES = frozenset({"curator_ledger"})
# migration 0027: accounts, their owners' policies, and the account each run took
ACCOUNT_TABLES = frozenset({"accounts", "account_policies", "run_accounts"})
# migration 0030: agent profiles and their versions
PROFILE_TABLES = frozenset({"agent_profiles", "agent_profile_versions"})
# migration 0031: group chats, their members, messages, mentions, reactions and events
CHAT_TABLES = frozenset({"chats", "chat_members", "chat_messages", "chat_mentions", "chat_reactions", "chat_events"})
# migration 0034: the store of plugins and their versions
PLUGIN_TABLES = frozenset({"plugins", "plugin_versions"})
# migration 0035: the plugins projects enabled, the versions of their config layers, and the plugins of a run
PLUGIN_PROJECT_TABLES = frozenset({"project_plugins", "plugin_configs", "run_plugins"})
# migration 0036: the tokens of plugin backends and what the plugin host says of each backend
PLUGIN_BACKEND_TABLES = frozenset({"plugin_tokens", "plugin_backends"})
# migration 0037: the versions of the hub's own settings, its branding and which features are on
SETTINGS_TABLES = frozenset({"hub_settings"})
# migration 0038: the outbox of the events and notifications the hub delivers to plugins
PLUGIN_POINT_TABLES = frozenset({"plugin_deliveries"})
# migration 0042: the agent types of plugins projects enabled
AGENT_TYPE_TABLES = frozenset({"project_agent_types"})
# migration 0043: the proposals of @builder, which an admin approves before the hub applies them
BUILDER_TABLES = frozenset({"builder_proposals"})
# migration 0044: the triage chats of failed runs and the triage thread of each failed run
TRIAGE_TABLES = frozenset({"triage_chats", "run_triage"})
# migration 0046: the BA documents of the core plugin srs, their versions, items, codes, reviews and comments
SRS_TABLES = frozenset(
    {
        "srs_documents",
        "srs_versions",
        "srs_texts",
        "srs_items",
        "srs_codes",
        "srs_item_versions",
        "srs_refs",
        "srs_reviews",
        "srs_comments",
    }
)
# migration 0047: the tasks of the core plugin tasks, their sprints, people, links, history and comments
TASKS_TABLES = frozenset(
    {"tasks_sprints", "tasks_items", "tasks_people", "tasks_links", "tasks_events", "tasks_comments"}
)
# migration 0052: the transcripts of sessions and their parts
TRANSCRIPT_TABLES = frozenset({"session_transcripts", "transcript_parts"})
# migration 0054: the deterministic facets of sessions and runs
FACET_TABLES = frozenset({"session_facets"})
# migration 0055: the files each git source holds in a project's latest graph, and each measure of the graph's health
KG_HEALTH_TABLES = frozenset({"kg_source_files", "kg_health"})
# migration 0056: each project's suite of replay tasks, and the replays of the Curator's changes
REPLAY_TABLES = frozenset({"replay_tasks", "curator_replays"})
# migration 0059: the history runs, and the groups of the facets of the model
HISTORY_TABLES = frozenset({"history_runs", "facet_clusters"})
# migration 0060: the words of the history for search, and the playbook of the Curator with its deltas
SEARCH_TABLES = frozenset({"history_lines", "history_indexed", "curator_playbook", "curator_playbook_deltas"})
# migration 0065: the commit of the tag nightly the hub last read
NIGHTLY_TABLES = frozenset({"hub_nightly"})
# migration 0067: the release schedules of projects, their cycles, the runs of each cycle and every check
RELEASE_TABLES = frozenset({"release_schedules", "release_cycles", "release_runs", "release_tags", "release_checks"})
# migration 0068: the emails members listed, one member an address
USER_EMAIL_TABLES = frozenset({"user_emails"})
# migration 0069: the conflicts @ba found in the BA documents of the core plugin srs
SRS_FINDING_TABLES = frozenset({"srs_findings"})
# migration 0070: the settle chat of each version of a BA document whose review left conflicts to settle
SRS_SETTLE_TABLES = frozenset({"srs_settle_chats"})
# migration 0071: what the approval of a version did to the project's tasks, and @ba's run that splits them
SRS_APPROVAL_TASK_TABLES = frozenset({"srs_approval_tasks"})
# migration 0098: the reviews of single use cases of a version of a BA document
SRS_USE_CASE_REVIEW_TABLES = frozenset({"srs_use_case_reviews"})
AGENT_QUESTION_TABLES = frozenset({"agent_questions"})  # the questions of core plugins to their agent types
# migration 0073: the canvases of the core plugin canvas, their claims, the revs of each claim and its evidence
CANVAS_TABLES = frozenset({"canvas_canvases", "canvas_claims", "canvas_claim_revs", "canvas_evidence"})
# migration 0074: the registry of a project's signal keys, its signals, and the ingest keys services write them with
SIGNAL_TABLES = frozenset({"signal_keys", "signals", "signal_ingest_keys"})
# migration 0075: what an agent or the hub proposes on a canvas, for a writer to accept or reject
CANVAS_PROPOSAL_TABLES = frozenset({"canvas_proposals"})
# migration 0076: the items judging runs of @strategist judged for the value check of canvas, with their verdicts
CANVAS_JUDGING_TABLES = frozenset({"canvas_judgings"})
# migration 0077: the baselines of canvas with what each asks of a claim, and the hub's events the Loop reads by cursor
CANVAS_BASELINE_TABLES = frozenset({"canvas_baselines", "canvas_baseline_items", "hub_events"})
# migration 0079: the revisions of each project's autonomy policy
POLICY_TABLES = frozenset({"autonomy_policies"})
# migration 0081: the merge queue on GitLab and its ledger
MERGE_TABLES = frozenset({"merge_queue", "merge_events"})
# migration 0083: the audit samples of the merge queue
SAMPLE_TABLES = frozenset({"merge_samples"})
# migration 0084: the breaker of each repo of the merge queue
BREAKER_TABLES = frozenset({"merge_breakers"})
# migration 0085: what a task waits on
TASKS_BLOCKER_TABLES = frozenset({"tasks_blockers"})
# migration 0086: what the plugin pm sent
PM_TABLES = frozenset({"pm_sends"})
# migration 0089: the outputs of runs and what carries them
OUTPUT_TABLES = frozenset({"run_outputs", "run_output_links"})
# migration 0090: the notices members turned off
MUTE_TABLES = frozenset({"notification_mutes"})
# migration 0091: the goals of a project and their revisions (plan loop-engine)
GOAL_TABLES = frozenset({"goals", "goal_revisions"})
# migration 0093: the Loop of a project, its work items and their ledger (plan loop-engine)
LOOP_TABLES = frozenset({"loop_states", "loop_items", "loop_item_events"})
# migration 0097: the keys of the notices an isolated plugin sends
PLUGIN_NOTICE_TABLES = frozenset({"plugin_notice_keys"})
# migration 0099: the org chart, its units, the seats in them and the revisions of their autonomy ceilings
ORG_TABLES = frozenset({"org_units", "org_seats", "autonomy_ceilings"})
# migration 0105: the shares of secrets and accounts and the projects of each (plan credential-sharing)
SHARE_TABLES = frozenset({"credential_shares", "credential_share_projects"})
# migration 0107: the units of the org chart a shared worker is narrowed to (plan credential-sharing)
WORKER_SHARE_TABLES = frozenset({"worker_shares"})
# migration 0109: the files people attach to messages, and the run inbox rows that carry them
ATTACHMENT_TABLES = frozenset({"attachments", "run_inbox_attachments"})
# migration 0110: the invite links
INVITE_TABLES = frozenset({"invites"})
# migration 0112: the revisions of each person's own autonomy preference (plan autonomy-levels)
AUTONOMY_LEVEL_TABLES = frozenset({"autonomy_prefs"})
# migration 0116: the stops of autonomy S4 (plan autonomy-levels)
AUTONOMY_STOP_TABLES = frozenset({"autonomy_stops"})
# migration 0117: the browser identities of projects and members (plan agent-browser)
BROWSER_TABLES = frozenset({"browser_identities"})
# migration 0124: the contacts of the core plugin crm (plan autonomous-oss-growth)
CRM_TABLES = frozenset({"crm_contacts", "crm_touchpoints", "crm_stage_history", "crm_cursors", "crm_deletions"})
# migration 0125: the channel accounts and the posting gate of the core plugin marketing
MARKETING_TABLES = frozenset(
    {
        "marketing_channels",
        "marketing_posts",
        "marketing_budget",
        "marketing_denylist",
        "marketing_state",
        "marketing_metrics",
    }
)
# migration 0127: the growth rules of marketing and crm on the Loop (plan autonomous-oss-growth, step 19)
GROWTH_TABLES = frozenset({"marketing_replies", "crm_invites_sent"})
PROBE_TABLES = frozenset({"curator_probes"})  # the probes of the Curator's proposals and changes (0130)
PROBE_RUN_TABLES = frozenset({"curator_probe_runs"})  # each probe measured once a week on each model (0131)
# migration 0134: the problems of a project and their revisions (plan problem-oriented-hub)
PROBLEM_TABLES = frozenset({"problems", "problem_revisions"})
PROBLEM_EVIDENCE_TABLES = frozenset({"problem_evidence"})  # migration 0137 (plan problem-intake)
INTAKE_TABLES = frozenset({"intakes"})  # migration 0138 (plan problem-intake)
# migration 0143: the opportunities of a project, their revisions, answers and evidence (plan opportunity-intake)
OPPORTUNITY_TABLES = frozenset(
    {"opportunities", "opportunity_revisions", "opportunity_answers", "opportunity_evidence"}
)
KG_REVIEW_TABLES = frozenset({"kg_review_dismissals"})  # migration 0141 (plan kg-map-and-review)
KG_DECISION_TABLES = frozenset({"kg_decisions", "kg_decision_events"})  # migration 0142 (plan kg-map-and-review)
# migration 0147: the rows of project_plugins of canvas, srs and tasks, once those became features (plan plugin-market)
RETIRED_PLUGIN_TABLES = frozenset({"retired_project_plugins"})
# migration 0154: the markets a hub reads, the publishers its admins trusted, install requests (plan plugin-market)
MARKET_TABLES = frozenset({"market_sources", "plugin_publishers", "market_install_requests"})
# migration 0159: the hub's id, a seller's listings, bundles, orders and payouts, a buyer's purchases (plan plugin-market)
SELLER_TABLES = frozenset(
    {"hub_identity", "market_listings", "market_bundles", "market_orders", "market_payouts", "market_purchases"}
)
# migration 0148: worker pools, the projects of each and their join tokens (plan worker-pools)
POOL_TABLES = frozenset({"worker_pools", "worker_pool_projects", "worker_join_tokens"})
PROVIDER_ACCOUNT_TABLES = frozenset({"provider_accounts"})  # migration 0151 (plan model-providers)
# migration 0156: the push devices of members and their notification preferences (plan hub-mobile-phase-0)
PUSH_TABLES = frozenset({"push_devices", "notification_preferences"})
TODAY_TABLES = frozenset({"today_marks"})  # migration 0158: what Today keeps of a member (plan hub-mobile-phase-0)
# migration 0163: the documents of a project, their versions, their images and the images of each version (feature docs)
DOCS_TABLES = frozenset({"docs_documents", "docs_versions", "docs_images", "docs_version_images"})
# migration 0165: the bridges of group chats to groups of a channel, their codes and the ids of what they carried
CHAT_BRIDGE_TABLES = frozenset({"chat_bridges", "chat_bridge_codes", "chat_bridge_messages"})
# migration 0169: the records an isolated plugin keeps on the hub, and the leases that say whom a backend's call is for
PLUGIN_RECORD_TABLES = frozenset({"plugin_records", "plugin_record_leases"})


def plugin_version_tables() -> frozenset[str]:
    """The version tables of the installed core plugins' chains (``plugin_schema.chains``), which ``migrate`` runs after
    the hub's own: crm, pm and marketing of evo-agents-core-plugins make one each and adopt their tables."""
    from evo_agents.hub import plugin_schema

    return frozenset(chain.version_table for chain in plugin_schema.chains())


def plugin_chain_tables() -> frozenset[str]:
    """The tables the installed core plugins' chains leave in a migrated database besides the hub's own: their version
    tables and the tables of their own metadata (``plugin_schema.installed_tables``), such as editorial's, which no
    chain of the hub made; the tables crm, pm and marketing adopt are the hub's as well."""
    from evo_agents.hub import plugin_schema

    return plugin_version_tables() | plugin_schema.installed_tables()


def plugin_revision_count() -> int:
    """How many revisions the installed core plugins' chains hold: ``migrate`` logs one upgrade line for each."""
    from evo_agents.hub import plugin_schema

    return sum(len(list(plugin_schema.versions_directory(chain).glob("[0-9]*.py"))) for chain in plugin_schema.chains())


HUB_ENV = ("EVO_HUB_",)  # variables a test environment must not inherit from the shell running pytest
AWAY = "_away"  # suffix of the copy a database waits in while set_reachable keeps it from its clients
END_EVERY = 0.05  # seconds between two rounds of ending the connections to a database being copied
TEMPLATE_EVERY = 10  # revision templates are made at every tenth revision (and head), not at each one a test needs
WHOLE_CHAIN_KEY = 0x65766F2D63686E  # "evo-chn": the advisory lock of one_whole_chain, on the server's own database


@dataclass(frozen=True)
class Database:
    name: str
    password: str
    dsn: str  # postgresql:// URI of the owner role, what the hub gets
    admin_dsn: str  # the superuser, connected to this database


def admin(dsn: str = DSN):
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def admin_engine(dsn: str = DSN):
    """A SQLAlchemy engine on ``dsn`` as the superuser, in autocommit like ``admin()``. It keeps no connection
    (``NullPool``): each ``connect()`` connects, and closes once its block ends."""
    if dsn not in _ADMIN_ENGINES:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        _ADMIN_ENGINES[dsn] = create_engine(
            "postgresql+psycopg://", creator=lambda: admin(dsn), poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
    return _ADMIN_ENGINES[dsn]


_ADMIN_ENGINES: dict = {}


def _activity():
    """The view of the server's backends, as much of it as this module reads."""
    from sqlalchemy import Text, column, table

    return table("pg_stat_activity", column("pid"), column("datname", Text))


def wait_ready(timeout: float = READY_TIMEOUT) -> None:
    """Return once the server answers a query; fail the session after ``timeout`` seconds."""
    from sqlalchemy import literal, select
    from sqlalchemy.exc import OperationalError

    deadline = time.monotonic() + timeout
    while True:
        try:
            with admin_engine().connect() as conn:
                conn.execute(select(literal(1)))
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                pytest.fail(f"Postgres at EVO_HUB_TEST_DSN not ready after {timeout:.0f}s: {exc.orig}")
            time.sleep(0.5)


def _new_owner(conn) -> tuple[str, str]:
    """A login role of its own with a random password; its name is also the name of its database."""
    from psycopg import sql

    name = f"evo_hub_test_{secrets.token_hex(6)}"
    password = f"Pw{secrets.token_hex(16)}"
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password)))
    return name, password


def _database(name: str, password: str) -> Database:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(DSN)
    host = quote(params.get("host") or "localhost", safe="")
    port = params.get("port") or "5432"
    dsn = f"postgresql://{name}:{password}@{host}:{port}/{name}"
    return Database(name, password, dsn, make_conninfo(DSN, dbname=name))


def create_database() -> Database:
    """An empty database, owned by a role of its own."""
    from psycopg import sql

    with admin() as conn:
        name, password = _new_owner(conn)
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
    return _database(name, password)


def make_template(db: Database) -> None:
    """Make ``db``, which its role has migrated, a template for ``clone_database``: wait for its last connection to
    end (``CREATE DATABASE ... TEMPLATE`` refuses a database anyone is connected to) and hand the database itself to
    the superuser. Its tables stay its role's; were the database its role's too, the ``REASSIGN OWNED`` of a clone,
    which also reaches the databases a role owns, would give this one away to the clone's role."""
    from psycopg import sql

    if left := wait_no_backends(db.name):
        pytest.fail(f"{left} connection(s) to the template {db.name} still open after migrating it")
    with admin() as conn:
        conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO CURRENT_USER").format(sql.Identifier(db.name)))


def clone_database(template: Database) -> Database:
    """A database of its own as ``template`` is, for a role of its own that owns everything in it: a copy of a
    migrated database costs a fraction of migrating an empty one. Dropped with ``drop_database`` as any other."""
    with admin() as conn:
        name, password = _new_owner(conn)
    db = _database(name, password)
    _create_copy(db, template)
    return db


def copy_into(db: Database, template: Database) -> None:
    """Make ``db`` a copy of ``template``, for its own role and password: drop it, connections and all, and create it
    again from the template."""
    from psycopg import sql

    with admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db.name)))
    _create_copy(db, template)


def _create_copy(db: Database, template: Database) -> None:
    from psycopg import sql

    with admin() as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(
                sql.Identifier(db.name), sql.Identifier(db.name), sql.Identifier(template.name)
            )
        )
    # The copy's tables, sequences, types and functions are still the template's role's: give them to the copy's.
    with admin(db.admin_dsn) as conn:
        conn.execute(
            sql.SQL("REASSIGN OWNED BY {} TO {}").format(sql.Identifier(template.name), sql.Identifier(db.name))
        )


def move(db: Database, revision: str, *, down: bool = False, in_steps: bool = False) -> tuple[str, ...]:
    """Upgrade ``db`` to ``revision`` with Alembic, or downgrade it, as ``alembic.command`` does but over scripts
    loaded once per process (loading them costs some 60 ms a move); the revisions upgraded, in order. Every revision
    commits together, as ``evo_agents.hub.migrate.migrate`` applies them, or with ``in_steps`` each on its own: one
    transaction keeps a lock on each table, index and sequence its revisions touch until it commits, some 2,700 for the
    whole chain, where one revision takes at most a few hundred."""
    from alembic.runtime.environment import EnvironmentContext

    from evo_agents.hub import migrate as hub_migrate

    script = _script()

    def steps(current, context):
        return script._downgrade_revs(revision, current) if down else script._upgrade_revs(revision, current)

    applied: list[str] = []
    engine = hub_migrate._engine(db.dsn)
    try:
        with engine.connect() as conn:
            config = hub_migrate.alembic_config()
            config.attributes.update(connection=conn, applied=applied, transaction_per_migration=in_steps)
            with EnvironmentContext(config, script, fn=steps, destination_rev=revision):
                script.run_env()
    finally:
        engine.dispose()
    return tuple(applied)


def migrate_in_steps(db: Database, revision: str = "head", *, down: bool = False) -> tuple[str, ...]:
    """``move`` with each revision committed on its own: the way along many revisions for tests that run at once."""
    return move(db, revision, down=down, in_steps=True)


@functools.cache
def _script():
    from alembic.script import ScriptDirectory

    from evo_agents.hub.migrate import alembic_config

    return ScriptDirectory.from_config(alembic_config())


@functools.cache
def revisions() -> tuple[str, ...]:
    """Every revision of the hub migrations, oldest first, as ``evo_agents.hub.migrate.revisions`` has them."""
    return tuple(reversed([revision.revision for revision in _script().walk_revisions()]))


_REVISION_TEMPLATES: dict[str, Database] = {}


def revision_template(revision: str) -> Database:
    """A template at ``revision`` ("head" or a revision number) for ``copy_into`` or ``clone_database``, made once per
    process: a copy of the nearest template below it moved up the revisions in between, so the templates of a pytest
    process migrate the chain once between them. ``drop_revision_templates`` drops them."""
    chain = revisions()
    revision = chain[-1] if revision == "head" else revision
    if revision in _REVISION_TEMPLATES:
        return _REVISION_TEMPLATES[revision]
    below = [rev for rev in chain[: chain.index(revision)] if rev in _REVISION_TEMPLATES]
    db = clone_database(_REVISION_TEMPLATES[below[-1]]) if below else create_database()
    try:
        migrate_in_steps(db, revision)
        make_template(db)
    except BaseException:
        drop_database(db)
        raise
    _REVISION_TEMPLATES[revision] = db
    return db


def template_below(revision: str) -> tuple[str, Database] | None:
    """The template of the revision at or below ``revision`` that is head or a multiple of ``TEMPLATE_EVERY``, and that
    revision; None below the first. A test that copies it moves up the few revisions left: a template of its own
    would cost one more copy."""
    chain = revisions()
    at = len(chain) - 1 if revision == "head" else chain.index(revision)
    if at != len(chain) - 1:
        at = (at + 1) // TEMPLATE_EVERY * TEMPLATE_EVERY - 1
    if at < 0:
        return None
    return chain[at], revision_template(chain[at])


def drop_revision_templates() -> None:
    while _REVISION_TEMPLATES:
        drop_database(_REVISION_TEMPLATES.popitem()[1])


@contextmanager
def one_whole_chain():
    """Wait until no other test on the server holds this lock, and hold it while the block runs: around a test that
    migrates the whole chain in one transaction, as ``evo_agents.hub.migrate.migrate`` does from an empty database."""
    from sqlalchemy import BigInteger, func, literal, select

    key = literal(WHOLE_CHAIN_KEY, BigInteger)
    with admin_engine().connect() as conn:  # the lock is the session's: closing the connection releases it too
        conn.execute(select(func.pg_advisory_lock(key)))
        try:
            yield
        finally:
            conn.execute(select(func.pg_advisory_unlock(key)))


def drop_database(db: Database) -> None:
    from psycopg import sql

    with admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name)))
        # the copy set_reachable keeps while the database is away, left by a test that failed before bringing it back
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db.name + AWAY)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(db.name)))


def backends(name: str) -> int:
    from sqlalchemy import func, select

    activity = _activity()
    with admin_engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(activity).where(activity.c.datname == name)).scalar_one()


def wait_no_backends(name: str, timeout: float = 5.0) -> int:
    """Connections still open to the database after ``timeout``; a backend exits shortly after its client."""
    deadline = time.monotonic() + timeout
    while (left := backends(name)) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


def _end_backends(name: str) -> None:
    """End every connection to the database ``name``."""
    from sqlalchemy import func, select

    activity = _activity()
    with admin_engine().connect() as conn:
        conn.execute(
            select(func.pg_terminate_backend(activity.c.pid)).where(
                activity.c.datname == name, activity.c.pid != func.pg_backend_pid()
            )
        )


def set_reachable(db: Database, reachable: bool) -> None:
    """Stop the database for its clients (refuse new connections, end the open ones), or bring it back with its
    data: Postgres going away, without stopping a server other tests share.

    Away, the database waits as a copy nobody may connect to, and its own name does not exist, so a client trying
    to connect is refused; back, it is created again from that copy, for the same owner. Copying needs the database
    to itself: while ``CREATE DATABASE ... TEMPLATE`` runs, a thread ends every connection to it, the ones open
    before and any a client opened again before the copy took its lock, which new connections then wait for. A
    write a client commits between the end of the copy and the drop is not in the copy."""
    from psycopg import sql

    name, away = sql.Identifier(db.name), sql.Identifier(db.name + AWAY)
    with admin() as conn:
        if reachable:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {}").format(name, name, away))
            conn.execute(sql.SQL("DROP DATABASE {}").format(away))
            return
        copied = threading.Event()

        def end_until_copied() -> None:
            while not copied.is_set():
                _end_backends(db.name)
                copied.wait(END_EVERY)

        ender = threading.Thread(target=end_until_copied, name=f"end-{db.name}", daemon=True)
        ender.start()
        try:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} ALLOW_CONNECTIONS false").format(away, name))
        finally:
            copied.set()
            ender.join()
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(name))


def free_port() -> int:
    """A port nothing listens on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(HUB_ENV) and k != "PGPASSWORD"}
    env.update(extra)
    if os.name == "nt" and "HOME" in extra:
        env["USERPROFILE"] = extra["HOME"]
    return env


def cli(args: list[str], env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess:
    """``evo-agents ARGS`` in a process of its own."""
    return subprocess.run(
        [sys.executable, "-m", "evo_agents", *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def log_lines(stderr: str) -> list[dict]:
    """The JSON object of every stderr line; fails the test on a line that is not one."""
    lines = []
    for line in stderr.splitlines():
        if line.strip():
            try:
                lines.append(json.loads(line))
            except ValueError:
                pytest.fail(f"stderr line is not a JSON object: {line!r}")
    return lines
