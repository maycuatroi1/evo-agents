"""A core plugin's own tables and chain of migrations (plan plugin-market, step 4): ``evo-agents hub migrate`` runs the
chain of northwind-ledger, the sample of tests/fixtures/core_plugins, after the hub's, recorded in a version table of
its own; its first revision adopts the table an older chain of the hub made without changing a row; a chain that does
not read is left out and the others still run; ``--plugin`` runs one chain alone and ``--status`` changes nothing."""

from __future__ import annotations

from argparse import Namespace
from datetime import date
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import func, insert, select

from evo_agents.hub import plugin_calls, plugin_schema
from evo_agents.hub import tables as hub_tables
from evo_agents.hub.cli import cmd_migrate
from evo_agents.hub.migrate import MigrationError, migrate, status
from evo_agents.hub.plugin_schema import PluginChain, PluginSchemaError
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP, core_plugins
from tests.hub import live
from tests.hub.conftest import hub_db, hub_template, pg_server  # noqa: F401 (fixtures)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "core_plugins"
LEDGER = EntryPoint("northwind-ledger", "northwind_ledger:manifest", ENTRY_POINT_GROUP)
VERSION_TABLE = "alembic_version_northwind_ledger"
HEAD = "northwind_ledger_0002"


@pytest.fixture
def ledger(monkeypatch):
    """northwind-ledger importable and the one core plugin of this process; its module of tables."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    monkeypatch.setattr(core, "entry_points", lambda *, group: [LEDGER] if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    from northwind_ledger import tables

    yield tables
    plugin_calls.core_registry.cache_clear()


def exists(db, name: str) -> bool:
    return live.sql(db, select(func.to_regclass(name)))[0][0] is not None


def recorded(db) -> list[str]:
    from sqlalchemy import column, table

    return [row[0] for row in live.sql(db, select(table(VERSION_TABLE, column("version_num")).c.version_num))]


def test_plugin_chain_adopts_the_table_an_older_chain_made_without_changing_a_row(hub_db, ledger):
    entries = ledger.entries
    with live.engine(hub_db).begin() as conn:
        entries.create(conn)  # as the hub's older chain left it
    u, p = hub_tables.users, hub_tables.projects
    [(user,)] = live.sql(hub_db, insert(u).values(login="lumen-owner").returning(u.c.id))
    label = {"level": "internal", "location": "any"}
    made = insert(p).values(
        name="lumen", levels=["public", "internal"], locations=["any"], default_label=label, created_by=user
    )
    [(project,)] = live.sql(hub_db, made.returning(p.c.id))
    rows = [
        {
            "project_id": project,
            "memo": "rent",
            "amount_cents": 120000,
            "due_on": date(2026, 1, 31),
            "created_by": user,
        },
        {"project_id": project, "memo": "fee", "amount_cents": -500, "due_on": None, "created_by": None},
    ]
    live.sql(hub_db, insert(entries), rows)
    before = live.sql(hub_db, select(entries).order_by(entries.c.id))
    assert not exists(hub_db, "northwind_ledger_notes")

    done = migrate(hub_db.dsn)

    assert done.applied == ()  # the hub's own chain was at head
    [chain] = done.plugins
    assert (chain.plugin_id, chain.before, chain.after, chain.error) == ("northwind-ledger", (), (HEAD,), None)
    assert chain.applied == ("northwind_ledger_0001", HEAD)
    assert live.sql(hub_db, select(entries).order_by(entries.c.id)) == before
    assert exists(hub_db, "northwind_ledger_notes") and recorded(hub_db) == [HEAD]
    again = migrate(hub_db.dsn)
    assert again.plugins[0].applied == () and again.plugins[0].after == (HEAD,)


def test_plugin_chain_creates_the_tables_it_adopts_where_they_are_missing(hub_db, ledger):
    assert not exists(hub_db, "northwind_ledger_entries")
    done = migrate(hub_db.dsn)
    assert done.plugins[0].after == (HEAD,)
    assert exists(hub_db, "northwind_ledger_entries") and exists(hub_db, "northwind_ledger_notes")


def test_plugin_chain_refuses_to_adopt_a_table_that_differs_and_commits_nothing(hub_db, ledger):
    from sqlalchemy import BigInteger, Column, MetaData, Table

    other = Table("northwind_ledger_entries", MetaData(), Column("id", BigInteger, primary_key=True))
    with live.engine(hub_db).begin() as conn:
        other.create(conn)
    done = migrate(hub_db.dsn)
    [chain] = done.plugins
    assert chain.after == () and "PluginSchemaError" in chain.error and "northwind_ledger_entries" in chain.error
    assert not exists(hub_db, VERSION_TABLE) and not exists(hub_db, "northwind_ledger_notes")
    with pytest.raises(MigrationError, match="core plugin northwind-ledger"):
        migrate(hub_db.dsn, only="northwind-ledger")


def test_a_plugin_chain_that_does_not_read_is_left_out_and_the_others_still_run(hub_db, ledger, monkeypatch):
    good = plugin_schema.chains(core_plugins())
    broken = PluginChain("lumen-broken", "lumen_nowhere.tables:metadata", "lumen_nowhere.migrations", ())
    monkeypatch.setattr(plugin_schema, "chains", lambda plugins=None: [broken, *good])
    done = migrate(hub_db.dsn)
    assert [chain.plugin_id for chain in done.plugins] == ["lumen-broken", "northwind-ledger"]
    assert "does not load" in done.plugins[0].error and done.plugins[1].after == (HEAD,)
    found = {chain.chain: chain for chain in status(hub_db.dsn)}
    assert found["lumen-broken"].head is None and "does not load" in found["lumen-broken"].error
    assert found["northwind-ledger"].recorded == (HEAD,) and found["northwind-ledger"].head == HEAD


def test_plugin_status_changes_nothing_and_migrate_plugin_runs_one_chain(hub_db, ledger, capsys):
    args = Namespace(dsn=hub_db.dsn, status=True, plugin=None)
    assert cmd_migrate(args) == 1  # the plugin's chain is behind
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("hub\talembic_version\t") and lines[0].endswith("\tat head")
    assert lines[1] == f"northwind-ledger\t{VERSION_TABLE}\t-\tbehind, head {HEAD}"
    assert not exists(hub_db, VERSION_TABLE)

    assert cmd_migrate(Namespace(dsn=hub_db.dsn, status=False, plugin="northwind-ledger")) == 0
    assert recorded(hub_db) == [HEAD]
    assert cmd_migrate(args) == 0
    assert capsys.readouterr().out.splitlines()[1] == f"northwind-ledger\t{VERSION_TABLE}\t{HEAD}\tat head"
    assert cmd_migrate(Namespace(dsn=hub_db.dsn, status=False, plugin="lumen-none")) == 1


def test_plugin_metadata_keeps_its_prefix_and_points_only_at_the_hubs_keys(ledger):
    from sqlalchemy import BigInteger, Column, ForeignKey, Table

    metadata = plugin_schema.plugin_metadata()
    Table("lumen_x", metadata, Column("id", BigInteger, primary_key=True))
    Table(
        "northwind_ledger_bad",
        metadata,
        Column("id", BigInteger, primary_key=True),
        Column("goal", BigInteger, ForeignKey("goals.id")),
    )
    found = plugin_schema.check_metadata("northwind-ledger", metadata)
    assert any("'lumen_x' must start with northwind_ledger_" in line for line in found)
    assert plugin_schema.check_metadata("northwind-ledger", ledger.metadata) == []
    with pytest.raises(PluginSchemaError):
        plugin_schema.load_metadata(PluginChain("northwind-ledger", "northwind_ledger.tables:nothing", "x", ()))
