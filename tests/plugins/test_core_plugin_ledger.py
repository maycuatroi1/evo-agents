"""northwind-ledger, the sample core plugin of API 1.3 (tests/fixtures/core_plugins/northwind_ledger), on a live hub
(plan plugin-market, step 4): its routes write its own tables, which its chain of migrations made, and reach the hub
through the API alone (a caller's access, the hub's reads, an event, a notice of its own kind, a join link, a build of
the knowledge graph, a task for its agent type, the configs of all projects, the audit log); its page is framed from
the files of its package with the hub's UI kit; its CLI commands come from the hub. Its chain of migrations is
``tests/hub/test_plugin_migrations.py``'s; what it may import ``test_core_plugin_imports.py``'s."""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import date, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import plugin_calls, tables
from evo_agents.hub.cli_plugin_commands import (
    CACHE_FILE,
    cached,
    register_plugin_commands,
    request_of,
    wants_refresh,
)
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import core_plugin_api as hub
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub.conftest import github, hub_db, hub_template, pg_server  # noqa: F401 (fixtures)
from tests.hub.live import sql
from tests.hub.test_curator import grant, night  # noqa: F401 (night: a fixture, on this module's web_client)
from tests.hub.test_runs import ADMIN, OTHER, OWNER, PLAN, PROJECT, READER

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "core_plugins"
PLUGIN = "northwind-ledger"
LEDGER = EntryPoint(PLUGIN, "northwind_ledger:manifest", ENTRY_POINT_GROUP)
ROUTES = f"/v1/x/{PLUGIN}/{PROJECT}"
ORIGIN = "https://hub.test"


@pytest.fixture
def installed(monkeypatch):
    """northwind-ledger importable and the one core plugin of this process."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    monkeypatch.setattr(core, "entry_points", lambda *, group: [LEDGER] if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def web_client(hub_db, tmp_path, github, installed):
    """A hub with northwind-ledger, its chain of migrations run as ``evo-agents hub migrate --plugin`` runs it."""
    assert migrate(hub_db.dsn, only=PLUGIN).plugins[0].after == ("northwind_ledger_0002",)
    changes = {**live.web_changes(github), "secrets_key": secrets.token_bytes(KEY_BYTES)}
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github, **changes)), base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def ledger(night, hub_db):
    """night's hub with the project enabling northwind-ledger."""
    client, headers = night.client, night.headers
    response = client.post(
        f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/enable", json={"permissions": []}, headers=headers["owner"]
    )
    assert response.status_code == 200, response.text
    night.db = hub_db
    return night


def add(client, headers, memo: str, cents: int, due_on: date | None = None):
    body = {"memo": memo, "amount_cents": cents, "due_on": due_on.isoformat() if due_on else None}
    return client.post(f"{ROUTES}/entries", json=body, headers=headers)


def on_hub(client, work):
    async def run():
        async with client.app.state.engine.begin() as conn:
            return await work(conn)

    return client.portal.call(run)


# Its own tables, and a caller's access


def test_core_plugin_routes_write_its_own_tables_as_the_callers_role_allows(ledger):
    client, headers, db = ledger.client, ledger.headers, ledger.db
    made = add(client, headers["other"], "rent", 120000, date(2026, 1, 31))
    assert made.status_code == 201, made.text
    entry = made.json()["id"]
    assert add(client, headers["reader"], "fee", 500).status_code == 403  # a reader reads
    assert add(client, headers["stranger"], "fee", 500).status_code == 404  # no grant: no such project
    listed = client.get(f"{ROUTES}/entries", headers=headers["reader"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == [
        {"id": entry, "memo": "rent", "amount_cents": 120000, "due_on": "2026-01-31", "paid": False}
    ]
    paid = client.post(f"{ROUTES}/entries/{entry}/paid", headers=headers["other"])
    assert paid.status_code == 200, paid.text
    assert client.get(f"{ROUTES}/entries", params={"open": "true"}, headers=headers["owner"]).json()["entries"] == []
    assert client.post(f"{ROUTES}/entries/{entry + 99}/paid", headers=headers["other"]).status_code == 404

    e, a = tables.hub_events, tables.audit
    assert sql(db, select(e.c.kind, e.c.payload).where(e.c.kind.like("plugin.%"))) == [
        ("plugin.northwind_ledger.entry_added", {"entry": entry})
    ]
    assert sql(db, select(a.c.target).where(a.c.action == "northwind-ledger.add")) == [
        (f"entry:{entry} project={PROJECT}",)
    ]

    response = client.post(f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/disable", headers=headers["owner"])
    assert response.status_code == 200, response.text
    assert client.get(f"{ROUTES}/entries", headers=headers["owner"]).status_code == 404  # not enabled: no route


def test_core_plugin_reads_the_hub_through_the_api(ledger):
    client, headers = ledger.client, ledger.headers
    found = client.get(f"{ROUTES}/overview", headers=headers["reader"])
    assert found.status_code == 200, found.text
    body = found.json()
    assert (body["project"], body["role"], body["you"]) == (PROJECT, "reader", READER)
    assert {OWNER, OTHER, READER} <= set(body["members"]) and PLAN in body["plans"]
    assert body["tasks"] == [] and body["goals"] == [] and isinstance(body["runs"], int)


# Notices, invites, the graph, tasks, configs


def test_core_plugin_sends_notices_of_its_own_kind_to_the_projects_members(ledger):
    client, headers, db = ledger.client, ledger.headers, ledger.db
    assert client.post(f"{ROUTES}/remind", headers=headers["other"]).json() == {"overdue": 0, "sent": 0}
    add(client, headers["other"], "rent", 100, date.today() - timedelta(days=3))
    sent = client.post(f"{ROUTES}/remind", headers=headers["other"])
    assert sent.status_code == 200, sent.text
    assert sent.json()["overdue"] == 1 and sent.json()["sent"] >= 3
    n, u = tables.notifications, tables.users
    rows = sql(db, select(u.c.login, n.c.notice_kind, n.c.link).join(u, u.c.id == n.c.user_id))
    assert {login for login, kind, link in rows if kind == "northwind_ledger_overdue"} >= {OWNER, OTHER, READER}
    assert {link for _, _, link in rows} == {f"/p/{PROJECT}/x/{PLUGIN}/ledger"}

    me = hub.plugin("northwind_ledger.server")
    with pytest.raises(ValueError, match="not a notice kind plugin northwind-ledger contributes"):
        on_hub(client, lambda conn: hub.notify(conn, me, PROJECT, "northwind_ledger_other", users=[1], title="x"))
    with pytest.raises(ValueError, match="a path of the hub's web"):
        on_hub(
            client,
            lambda conn: hub.notify(
                conn, me, PROJECT, "northwind_ledger_overdue", users=[1], title="x", link="https://elsewhere.test"
            ),
        )


def test_core_plugin_makes_invites_builds_tasks_and_reads_configs_as_the_api_allows(ledger):
    client, headers, db = ledger.client, ledger.headers, ledger.db
    assert client.post(f"{ROUTES}/invite", json={}, headers=headers["other"]).status_code == 403
    made = client.post(f"{ROUTES}/invite", json={"for_login": "lumen-new"}, headers=headers["owner"])
    assert made.status_code == 200, made.text
    invite = made.json()
    assert invite["token"] and invite["url"].startswith(ORIGIN)
    i, a = tables.invites, tables.audit
    assert sql(db, select(i.c.role, i.c.for_login).where(i.c.id == invite["id"])) == [("reader", "lumen-new")]
    assert sql(db, select(a.c.action).where(a.c.target.like(f"invite:{invite['id']} %"))) == [("invite.create",)]

    assert client.post(f"{ROUTES}/kg", headers=headers["other"]).status_code == 202

    handed = client.post(f"{ROUTES}/hand", json={"task": "check March"}, headers=headers["other"])
    assert handed.status_code == 200, handed.text
    assert handed.json()["enabled"] is False and handed.json()["run_id"] is None and handed.json()["refused"]

    assert client.get(f"{ROUTES}/configs", headers=headers["owner"]).status_code == 403  # a hub admin's
    grant(client, headers, ADMIN, "reader")  # the plugin's routes answer in a project the caller reaches
    configs = client.get(f"{ROUTES}/configs", headers=headers["admin"])
    assert configs.status_code == 200, configs.text
    assert configs.json() == {"projects": [PROJECT]}


# Its page


def test_core_plugin_page_is_framed_from_its_package_with_the_hubs_ui_kit(ledger):
    client, headers = ledger.client, ledger.headers
    shown = client.get(f"/v1/projects/{PROJECT}/plugins/{PLUGIN}", headers=headers["reader"])
    assert shown.status_code == 200, shown.text
    [page] = shown.json()["pages"]
    assert (page["name"], page["native"]) == ("ledger", False) and page["url"].endswith("/ledger.html")
    html = client.get(page["url"])
    assert html.status_code == 200, html.text
    assert html.headers["content-type"].startswith("text/html")
    assert "/v1/plugin-ui/kit/1/evo-ui.css" in html.text and "/v1/plugin-ui/kit/1/evo-ui.js" in html.text
    base = page["url"].rsplit("/", 1)[0]
    for name, words in (("i18n/en.js", "Ledger"), ("i18n/vi.js", "Sổ cái"), ("ledger.js", "evo.route")):
        found = client.get(f"{base}/{name}")
        assert found.status_code == 200 and words in found.text, name
    for path in ("..%2F__init__.py", "../tables.py", ".hidden.js", "missing.html", "i18n/ja.js"):
        assert client.get(f"{base}/{path}").status_code == 404, path
    forged = page["url"].replace(page["url"].split("/")[-2], "0" * 20)
    assert client.get(forged).status_code == 404


def test_plugin_ui_kit_is_served_to_any_origin_and_nothing_else(ledger):
    client = ledger.client
    for name, kind in (("evo-ui.css", "text/css"), ("evo-ui.js", "javascript")):
        found = client.get(f"/v1/plugin-ui/kit/1/{name}")
        assert found.status_code == 200 and kind in found.headers["content-type"], name
        assert found.headers["access-control-allow-origin"] == "*"
    for path in ("2/evo-ui.css", "1/evo-ui.txt", "1/..%2F..%2Fapp.py", "1/missing.js"):
        assert client.get(f"/v1/plugin-ui/kit/{path}").status_code == 404, path


# Its CLI commands


def test_plugin_cli_commands_come_from_the_hub_with_an_etag(ledger):
    client, headers = ledger.client, ledger.headers
    assert client.get("/v1/cli/plugins").status_code == 401
    found = client.get("/v1/cli/plugins", headers=headers["reader"])
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["version"] == 1
    [plugin] = body["plugins"]
    assert (plugin["id"], plugin["version"]) == (PLUGIN, "1.0.0")
    assert [command["name"] for command in plugin["commands"]] == ["list", "add", "remind"]
    etag = found.headers["etag"]
    again = client.get("/v1/cli/plugins", headers={**headers["reader"], "If-None-Match": etag})
    assert again.status_code == 304 and again.headers["etag"] == etag
    assert client.get("/v1/cli/plugins", headers={**headers["reader"], "If-None-Match": '"x"'}).status_code == 200


def test_plugin_cli_commands_parse_and_call_the_plugins_routes(ledger, tmp_path, monkeypatch):
    client, headers = ledger.client, ledger.headers
    plugins = client.get("/v1/cli/plugins", headers=headers["owner"]).json()["plugins"]
    monkeypatch.setenv("EVO_HUB_HOME", str(tmp_path))
    (tmp_path / CACHE_FILE).write_text(json.dumps({"plugins": plugins}), encoding="utf-8")
    assert cached() == {"plugins": plugins}

    from evo_agents.cli import build_parser

    args = build_parser().parse_args(
        ["hub", PLUGIN, "add", "--project", PROJECT, "rent", "--amount-cents", "1500", "--due-on", "2026-02-01"]
    )
    method, path, body = request_of(args.plugin_id, args.plugin_spec, vars(args))
    assert (method, path) == ("POST", f"{ROUTES}/entries")
    assert body == {"memo": "rent", "amount_cents": 1500, "due_on": "2026-02-01"}
    made = client.request(method, path, json=body, headers=headers["owner"])
    assert made.status_code == 201, made.text

    args = build_parser().parse_args(["hub", PLUGIN, "list", "--project", PROJECT, "--open"])
    method, path, body = request_of(args.plugin_id, args.plugin_spec, vars(args))
    assert (method, path, body) == ("GET", f"{ROUTES}/entries?open=true", None)
    assert [row["memo"] for row in client.get(path, headers=headers["owner"]).json()["entries"]] == ["rent"]


def test_plugin_cli_asks_the_hub_again_only_for_a_command_it_has_no_parser_for():
    plugins = [{"id": PLUGIN, "commands": [{"name": "list"}]}]
    own = {"migrate", "plan"}
    assert not wants_refresh(["hub", "plan", "list"], own, plugins)
    assert not wants_refresh(["hub", PLUGIN, "list"], own, plugins)
    assert not wants_refresh(["hub", PLUGIN, "--help"], own, plugins)
    assert not wants_refresh(["hub"], own, plugins) and not wants_refresh(["harness", "x"], own, plugins)
    assert wants_refresh(["hub", PLUGIN, "add"], own, plugins)  # a command the plugin added since
    assert wants_refresh(["hub", "lumen-books", "list"], own, plugins)  # a plugin installed since


def test_plugin_cli_leaves_out_a_plugin_named_as_a_command_of_the_clis_own():
    parser = argparse.ArgumentParser()
    hsub = parser.add_subparsers(dest="hub_command")
    hsub.add_parser("migrate")
    command = {"name": "list", "http": {"method": "GET", "path": "entries"}}
    register_plugin_commands(hsub, [{"id": "migrate", "commands": [command]}, {"id": PLUGIN, "commands": [command]}])
    assert set(hsub.choices) == {"migrate", PLUGIN}
    assert parser.parse_args([PLUGIN, "list"]).plugin_id == PLUGIN


def test_core_plugin_pushes_a_signal_as_an_ingest_key_named_after_it(ledger):
    client, headers, db = ledger.client, ledger.headers, ledger.db
    key = {"name": "ledger.open", "kind": "number"}
    declared = client.post(f"/v1/projects/{PROJECT}/signal-keys", json=key, headers=headers["owner"])
    assert declared.status_code == 201, declared.text
    body = {"key": "ledger.open", "value": 3, "external_id": "march"}
    first = client.post(f"{ROUTES}/signal", json=body, headers=headers["other"])
    assert first.status_code == 200, first.text
    assert first.json() == {"stored": True, "duplicate": False, "refused": None}
    again = client.post(f"{ROUTES}/signal", json=body, headers=headers["other"])
    assert again.json() == {"stored": False, "duplicate": True, "refused": None}
    k = tables.signal_ingest_keys
    assert sql(db, select(k.c.name).where(k.c.revoked_at.is_(None))) == [(PLUGIN,)]
    me = hub.plugin("northwind_ledger.server")
    values = on_hub(client, lambda conn: hub.read_signals(conn, me, PROJECT, "ledger.open"))
    assert [value.value for value in values] == [3]
