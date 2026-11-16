"""POST /v1/kg/{project}/grep-links (``evo_agents.hub.server.kg_grep``): what the hub's graph links to the lines a grep
matched, the note of ``evo-agents kg hook post-search``.

The demo project of ``tests.kg.projects`` is built in memory (``tests.kg.built``), its app repo labelled customer, and
its rows are the hub's latest successful build. Checked: a member cleared for customer through the agents' sink gets
the line the hook test expects (``tests.kg.test_hooks.SEARCH``); a member whose grant stops at internal, a sink that
clears internal only and a sink the project does not declare get nothing; a non-member is told no such project; a hit naming a
file the request lacks is refused; a project with no graph yet is 409; and a KG run's token reaches the route.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, literal, select

from evo_agents.hub import kg_graph_rows, tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import kg_run_access
from evo_agents.hub.server.app import create_app
from tests.hub.test_kg_postgres_build import make_engine
from tests.kg.built import rows_of, sync_build
from tests.kg.projects import commit, make_project, write

PROJECT = "demo"
EMPTY = "lumen-empty"
AGENT = "claude-code@anthropic"
LEVELS = ["public", "internal", "customer"]
REGISTRATION = {
    "levels": LEVELS,
    "locations": ["any"],
    "default_label": {"level": "internal"},
    "sinks": [
        {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
        {"id": "narrow", "kind": "agent-session", "clearance": {"level": "internal"}},
        {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
    ],
    "repos": [{"name": "app", "path": "demo-app", "origin": "https://git.lumenops.example/demo/app.git"}],
}
GRANTS = {"alice": ("reader", "customer"), "carol": ("reader", "internal")}
TESTS = "from app.search import search\n\n\ndef test_search_ranks():\n    assert search('a') == ['a']\n"
SEARCH = (
    "- app:app/search.py::search: callers app:app/server.py::Server.handle; "
    "tests app:tests/test_search.py::test_search_ranks; plan steps plan:demo/step:1"
)
BODY = {
    "sink": AGENT,
    "files": [{"repo": "app", "path": "app/search.py"}],
    "hits": [{"file": 0, "line": 5, "text": "def search(query):"}],
}


def install(db, project) -> None:
    """The project's build in memory as the hub's latest successful build of demo, in its graph tables."""
    report = sync_build(project)
    projects = tables.projects
    found = select(
        projects.c.id,
        literal("succeeded"),
        literal(report.content_hash),
        literal(report.nodes),
        literal(report.edges),
        func.now(),
        func.now(),
    ).where(projects.c.name == PROJECT)
    columns = ["project_id", "status", "content_hash", "nodes", "edges", "started_at", "finished_at"]
    built = insert(tables.kg_builds).from_select(columns, found)
    ((build_id, project_id),) = live.sql(db, built.returning(tables.kg_builds.c.id, tables.kg_builds.c.project_id))

    async def keep():
        engine = make_engine(db)
        try:
            async with engine.begin() as conn:
                await kg_graph_rows.write_graph(conn, project_id, build_id, rows_of(project))
        finally:
            await engine.dispose()

    asyncio.run(keep())


@pytest.fixture
def hub(hub_db, tmp_path, monkeypatch):
    home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(home))  # never this machine's kg home
    project = make_project(tmp_path, home, app_level="customer")
    write(tmp_path / "demo-app" / "tests/test_search.py", TESTS)
    commit(tmp_path / "demo-app")
    config = HubConfig(
        dsn=hub_db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=3.0,
        admins=frozenset({live.ADMIN}),
        public_url="https://hub.test",
    )
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        admin = live.bearer(live.insert_token(hub_db, live.ADMIN))
        for name in (PROJECT, EMPTY):
            response = client.put(
                f"/v1/projects/{name}",
                json={**REGISTRATION, "harness": {"name": name, "workspace": "~/ws", "path": f"{name}-harness"}},
                headers=admin,
            )
            assert response.status_code == 200, response.text
        members = {"eve": live.bearer(live.insert_token(hub_db, "eve"))}
        for login, (role, level) in GRANTS.items():
            for name in (PROJECT, EMPTY):
                grant = {"role": role, "max_level": level}
                response = client.put(f"/v1/admin/projects/{name}/grants/{login}", json=grant, headers=admin)
                assert response.status_code == 200, response.text
            members[login] = live.bearer(live.insert_token(hub_db, login))
        install(hub_db, project)
        yield SimpleNamespace(client=client, **members)


def links(hub, login: str, project: str = PROJECT, status: int = 200, **changes):
    response = hub.client.post(f"/v1/kg/{project}/grep-links", json={**BODY, **changes}, headers=getattr(hub, login))
    assert response.status_code == status, response.text
    return response.json()


def test_grep_links_answers_the_note_within_the_readers_clearance(hub):
    assert links(hub, "alice") == {"lines": [SEARCH]}
    assert links(hub, "carol") == {"lines": []}  # the app repo is customer, carol's grant stops at internal
    assert links(hub, "alice", sink="narrow") == {"lines": []}  # a sink that clears internal only
    assert links(hub, "alice", sink="undeclared") == {"lines": []}
    assert links(hub, "alice", files=[{"repo": "app", "path": "app/missing.py"}]) == {"lines": []}


def test_grep_links_refuses_what_it_cannot_answer(hub):
    links(hub, "eve", status=404)  # no grant: the project is not one eve can see
    links(hub, "alice", project=EMPTY, status=409)  # no graph yet
    links(hub, "alice", status=422, hits=[{"file": 1, "line": 5, "text": "def search(query):"}])
    links(hub, "alice", status=422, files=[{"repo": "app", "path": f"f{n}.py"} for n in range(101)])


def test_a_kg_runs_token_reaches_grep_links_of_its_project_alone():
    assert kg_run_access.refusal("POST", f"/v1/kg/{PROJECT}/grep-links", PROJECT) is None
    assert kg_run_access.refusal("POST", f"/v1/kg/{EMPTY}/grep-links", PROJECT) is not None
    assert kg_run_access.refusal("GET", f"/v1/kg/{PROJECT}/grep-links", PROJECT) is not None
