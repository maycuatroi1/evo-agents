"""The metadata of ``evo_agents.hub.tables`` is the schema the migrations make.

Alembic's ``compare_metadata``, comparing types and server defaults, finds no difference on a database migrated to
head, procrastinate's tables aside; the migration environment autogenerates from that metadata and finds nothing to
do. What Alembic compares: tables, columns, types, nullability, server defaults, identities, primary, unique and
foreign keys, and indexes with their columns, expressions and order. What it does not: CHECK constraints, triggers
and the WHERE of a partial index, which the migrations alone hold (``tests.hub.test_migrate`` and
``tests.hub.test_run_tables`` exercise them).
"""

import gc
import warnings

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from evo_agents.hub import tables
from evo_agents.hub.migrate import alembic_config, migrate
from evo_agents.hub.migrations.owned import PLUGIN_OWNED_TABLES
from evo_agents.hub.plugin_schema import installed_tables
from tests.hub.test_migrate import TABLES

# Alembic compares a generated column's expression as text and warns at the casts Postgres adds to it.
GENERATED_COLUMN_WARNING = "memories.search"


def engine_on(db):
    return create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(db.dsn), poolclass=NullPool)


def _plugins(name: str | None) -> bool:
    """A table crm, pm or marketing keeps, or the version table of a core plugin's chain (``alembic_version_<id>``),
    as the migration environment leaves them out (``env.py``), with the tables of the core plugins installed here
    (editorial and okr keep theirs, which their own chains migrate)."""
    return name is not None and (
        name in PLUGIN_OWNED_TABLES or name in installed_tables() or name.startswith("alembic_version_")
    )


def include_name(name, type_, parent_names) -> bool:
    if type_ == "table":
        return not (name.startswith("procrastinate_") or _plugins(name))
    return not _plugins(parent_names.get("table_name"))


def include_object(item, name, type_, reflected, compare_to) -> bool:
    table = name if type_ == "table" else getattr(getattr(item, "table", None), "name", None)
    return not _plugins(table)


def hub_tables() -> set[str]:
    """The tables of the hub's metadata that are the hub's own: crm, pm and marketing keep theirs (owned.py)."""
    return set(tables.metadata.tables) - PLUGIN_OWNED_TABLES


def test_the_metadata_holds_the_one_hundred_and_ninety_six_hub_tables():
    assert hub_tables() == TABLES - pg.QUEUE_TABLES - PLUGIN_OWNED_TABLES
    # the last added: problems, problem_revisions, problem_evidence, intakes, kg_review_dismissals, kg_decisions,
    # kg_decision_events, opportunities (4), retired_project_plugins, worker_pools, worker_pool_projects,
    # worker_join_tokens, provider_accounts, market_sources, plugin_publishers, market_install_requests, push_devices,
    # notification_preferences, today_marks, hub_identity, market_listings, market_bundles, market_orders,
    # market_payouts, market_purchases, docs_documents, docs_versions, docs_images, docs_version_images, chat_bridges,
    # chat_bridge_codes, chat_bridge_messages, plugin_records, plugin_record_leases, srs_delivery, srs_baselines, context_answer_loads, kg_query_labels, kg_eval_sets, kg_semantic, kg_optimize_runs, merge_checks, proposal_threads; 210 with the 14 tables of crm, pm and marketing, whose plugins keep them
    assert len(hub_tables()) == 196
    assert len(PLUGIN_OWNED_TABLES) == 14 and PLUGIN_OWNED_TABLES <= TABLES


def test_the_metadata_matches_the_schema_at_head(hub_db):
    migrate(hub_db.dsn)
    engine = engine_on(hub_db)
    # what earlier tests of this worker left for the collector (an sqlite connection of a knowledge graph) is
    # collected now, so its ResourceWarning is not recorded as one of the comparison's
    gc.collect()
    try:
        with engine.connect() as conn, warnings.catch_warnings(record=True) as warned:
            warnings.simplefilter("always")
            context = MigrationContext.configure(
                conn,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_name": include_name,
                    "include_object": include_object,
                },
            )
            diff = compare_metadata(context, tables.metadata)
    finally:
        engine.dispose()
    assert diff == []
    assert [str(w.message) for w in warned if GENERATED_COLUMN_WARNING not in str(w.message)] == []


@pytest.mark.filterwarnings(f"ignore:Computed default on {GENERATED_COLUMN_WARNING}")
def test_autogenerate_reads_the_metadata_and_finds_nothing_to_do(hub_db):
    migrate(hub_db.dsn)
    config = alembic_config()
    engine = engine_on(hub_db)
    try:
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.check(config)  # AutogenerateDiffsDetected on any difference
    finally:
        engine.dispose()
