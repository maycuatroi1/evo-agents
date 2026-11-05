"""The run that maps a project's entities to its code (plan kg-entities-to-code, criterion a3): when a build asks for
one, what the run's sink reads of a graph (the outline, what changed, the counts of the prompt) without the database;
on Postgres, the route and the build that queue it, one run a project at a time under the review cost cap, the review
run's prompt (counts, never an item), the end of the run and what its token reaches."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import kg_entity_map_run as model
from evo_agents.hub import review as review_model
from evo_agents.kg.build import build_project
from evo_agents.kg.store import Store
from tests.hub import pg
from tests.hub.test_kg_eval_bootstrap import kg_env  # noqa: F401 (a fixture)
from tests.kg.test_entity_map import CLUSTER, GUIDE, SEARCH_FILE, SERVER, a_map, put_map, setup

EVERYTHING = (100, 100)  # a ceiling above every label of the test graph
NOTHING = (-1, -1)  # one below every label
NOW = datetime(2026, 11, 6, tzinfo=UTC)


@pytest.fixture
def store(tmp_path, kg_env):  # noqa: F811
    project = setup(tmp_path, kg_env)
    put_map(project, a_map())
    build_project(project)
    opened = Store.open_file(project.root / "graph.sqlite")
    yield opened
    opened.close()


# Without the database


def test_the_lens_is_on_demand_and_migration_0210_holds_the_lenses():
    from importlib import import_module

    assert model.LENS in review_model.LENSES and model.LENS in review_model.ON_DEMAND_LENSES
    assert model.LENS not in review_model.lenses_for(NOW.date(), len(review_model.LENSES))
    migration = import_module("evo_agents.hub.migrations.versions.0210_kg_entity_map")
    assert sorted(migration.LENSES) == sorted(review_model.LENSES)
    assert (migration.STATES, migration.VIAS) == (model.STATES, model.VIAS)


def test_a_build_asks_for_a_run_first_under_the_strategy_then_when_citations_go_stale_or_uncited():
    mapped = {"enabled": True, "maps": 0, "mentions": 0, "stale": 0, "cited_items": 0, "uncited_items": 0}
    assert model.wants_run({}, had_run=False, last_asked=None, now=NOW) is None  # another strategy
    assert (
        model.wants_run({"entity_map": {**mapped, "enabled": False}}, had_run=False, last_asked=None, now=NOW) is None
    )
    via, why = model.wants_run({"entity_map": mapped}, had_run=False, last_asked=None, now=NOW)
    assert via == "first_build" and "entity-graph-agent" in why[0]
    fresh = {**mapped, "mentions": 10, "stale": 2, "cited_items": 6, "uncited_items": 4}
    assert model.wants_run({"entity_map": fresh}, had_run=True, last_asked=None, now=NOW) is None
    stale = {**fresh, "stale": 3}
    via, why = model.wants_run({"entity_map": stale}, had_run=True, last_asked=NOW - timedelta(days=2), now=NOW)
    assert via == "stale" and why == ["3 of 10 citations are stale, over 20%"]
    uncited = {**fresh, "uncited_items": 7}
    assert model.wants_run({"entity_map": uncited}, had_run=True, last_asked=None, now=NOW)[1][0].startswith("7 of 13")
    recent = NOW - model.COOLDOWN + timedelta(minutes=1)
    assert model.wants_run({"entity_map": stale}, had_run=True, last_asked=recent, now=NOW) is None


def test_the_outline_holds_the_tree_clusters_entities_documents_and_plans_the_sink_reads(store):
    found = model.outline(store, EVERYTHING)
    counts = found["counts"]
    assert counts["files"] >= 3 and counts["symbols"] >= 4 and counts["entities"] == 3
    app = next(d for d in found["directories"] if (d["repo"], d["path"]) == ("app", "app"))
    assert app["files"] == 3 and app["symbols"] == 4
    assert next(r for r in found["repos"] if r["repo"] == "app") == {"repo": "app", "files": 3, "symbols": 4}
    server = next(e for e in found["entities"] if e["name"] == "Server")
    assert server["id"].startswith(model.ENTITY_PREFIX) and server["code"] == 2
    assert any(d["id"] == GUIDE and d["sections"] >= 1 for d in found["documents"])
    assert found["plans"] == [{"id": "plan:demo", "name": "demo", "steps": 2}]
    assert found["clusters"] and all(
        c["code"] and set(c) == {"id", "level", "code", "other", "members"} for c in found["clusters"]
    )
    assert any(SEARCH_FILE in c["members"] for c in found["clusters"])


def test_nothing_above_the_sink_is_outlined_or_counted(store):
    for ceiling in (NOTHING, None):  # None: the sink's grant clears no label
        found = model.outline(store, ceiling)
        assert set(found["counts"].values()) == {0} and found["clusters"] == found["directories"] == []
        readable, revs = model.readable(store, ceiling, None)
        assert revs == {} and readable.items == readable.entities == readable.citable == 0
    readable, _ = model.readable(store, NOTHING, None)
    assert readable.above == model.readable(store, EVERYTHING, None)[0].items > 0


def test_what_changed_since_the_last_map_and_what_no_map_cites(store):
    readable, revs = model.readable(store, EVERYTHING, None)
    assert readable.changed == {"new": len(revs), "changed": 0, "gone": 0} and readable.uncited < readable.citable
    assert "commit" not in readable.classes and "binary" not in readable.classes
    nothing = model.changed(store, EVERYTHING, revs)
    assert nothing["since_map"] and nothing["counts"]["new"] == nothing["counts"]["changed"] == 0
    item = sorted(revs)[0]
    before = {**{k: v for k, v in revs.items() if k != item}, "gone:item": "1", sorted(revs)[1]: "other"}
    found = model.changed(store, EVERYTHING, before, limit=1)
    assert found["counts"] == {"new": 1, "changed": 1, "gone": 1, "uncited": readable.uncited}
    assert found["new"] == [{"id": item, "class": found["new"][0]["class"]}] and found["gone"][0]["id"] == "gone:item"
    assert len(found["uncited"]) == 1 and GUIDE not in {
        u["id"] for u in model.changed(store, EVERYTHING, None)["uncited"]
    }
    assert not model.changed(store, EVERYTHING, None)["since_map"]


def test_the_privacy_of_the_prompt_counts_and_classes_never_an_item(store):
    readable, _ = model.readable(store, EVERYTHING, None)
    text = model.prompt(
        project="demo", run_id=7, sink="claude-code@anthropic", found=readable, why=["a writer asked"], since_map=False
    )
    assert f"holds {readable.items} items" in text and "code (" in text and "No map of the project" in text
    assert "--outline --json" in text and "--changed --json" in text and "--post FILE" in text
    assert "Directory" in text and "Symbol" in text and "subagents" in text and "Record no finding" in text
    for named in (GUIDE, CLUSTER, SERVER, SEARCH_FILE, "Server", "Ranking", "guide.md", "search.py"):
        assert named not in text


# On Postgres

if pg.DSN:
    from functools import partial
    from types import SimpleNamespace

    from sqlalchemy import func, select, update

    from evo_agents.hub import kg_entity_map_db, kg_init, tables
    from evo_agents.hub.server import kg_entity_map_run as server
    from evo_agents.hub.server import kg_run_access
    from evo_agents.hub.server.collect import review_prompt
    from tests.hub.live import sql
    from tests.hub.test_curator import PROJECT, night, set_run  # noqa: F401 (night is a fixture)
    from tests.hub.test_kg_optimize import built, project_id
    from tests.hub.test_review_runs import review  # noqa: F401 (a fixture)
    from tests.hub.test_runs import web_client  # noqa: F401 (a fixture)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
URL = f"/v1/kg/{PROJECT if pg.DSN else 'x'}/entity-map"
SECRET = "secret-customer-ledger.py"
READS = {"items": 12, "above": 2, "classes": {"code": 3, "prose": 6}, "entities": 0, "citable": 9, "uncited": 9}


async def reads(engine, row, blobs, data_dir, before):
    found = {**READS, "changed": {"new": 12, "changed": 0, "gone": 0}}
    return {"sink": "claude-code@anthropic", "readable": found, "revs": {f"app:file:{SECRET}": "abc"}}


async def unreadable(engine, row, blobs, data_dir, before):
    return {"why": "the project has no graph on the hub yet"}


def mapped_build(db, enabled=True, **counts) -> int:
    """A succeeded build of the project whose coverage is of strategy entity-graph-agent (``enabled``)."""
    build = built(db, project_id(db))
    b = tables.kg_builds
    coverage = {"sources": 1, "entity_map": {"enabled": enabled, "maps": 0, **counts}}
    sql(db, update(b).where(b.c.id == build).values(coverage=coverage))
    return build


def map_row(db, run_id: int | None = None) -> dict:
    t = tables.kg_entity_map_runs
    query = select(t).order_by(t.c.id.desc()).limit(1) if run_id is None else select(t).where(t.c.id == run_id)
    (row,) = sql(db, query)
    return dict(zip(t.c.keys(), row, strict=True))


def advanced(world, count=reads, now=None) -> dict:
    engine = world.client.app.state.engine
    return world.client.portal.call(partial(server.advance, engine, count=count, now=now))


def prompt_of(world, run_id: int) -> str:
    async def read():
        view = SimpleNamespace(id=run_id, project=PROJECT, repos=[])
        async with world.client.app.state.engine.begin() as conn:
            return await review_prompt(conn, view)

    return world.client.portal.call(read)


def on_build(world, build: int, coverage: dict):
    async def call():
        async with world.client.app.state.engine.begin() as conn:
            return await kg_entity_map_db.on_build(conn, project_id(world.db), build, coverage)

    return world.client.portal.call(call)


@needs_pg
def test_a_writer_asks_for_a_run_and_a_second_call_finds_the_one_in_flight(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    assert client.post(f"{URL}/run", headers=owner).status_code == 409  # no build of the strategy yet
    mapped_build(db, enabled=False)
    refused = client.post(f"{URL}/run", headers=owner)
    assert refused.status_code == 409 and "entity-graph-agent" in refused.json()["message"]
    build = mapped_build(db)
    assert client.post(f"{URL}/run", headers=review.headers["reader"]).status_code == 403
    first = client.post(f"{URL}/run", headers=owner)
    assert first.status_code == 201, first.text
    run = first.json()
    assert (run["via"], run["state"], run["build_id"], run["maps"]) == ("manual", "queued", build, 0)
    again = client.post(f"{URL}/run", headers=owner)
    assert again.status_code == 200 and again.json()["id"] == run["id"]
    listed = client.get(f"{URL}/runs", headers=review.headers["reader"]).json()
    assert listed["active"]["id"] == run["id"] and [r["id"] for r in listed["runs"]] == [run["id"]]


@needs_pg
def test_the_run_queues_one_review_run_under_the_cap_and_its_prompt_holds_counts_only(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    mapped_build(db)
    run_id = client.post(f"{URL}/run", headers=owner).json()["id"]
    assert advanced(review) == {"queued": 1}
    row = map_row(db, run_id)
    assert (
        row["state"] == "running" and row["agent_run_id"] is not None and row["revs"] == {f"app:file:{SECRET}": "abc"}
    )
    r = tables.runs
    (run,) = sql(db, select(r.c.kind, r.c.title, r.c.budget, r.c.state).where(r.c.id == row["agent_run_id"]))
    assert (run[0], run[3]) == ("review", "queued") and run[1] == f"KG entity map of {PROJECT}"
    assert run[2]["max_usd"] <= kg_init.HUB_CAP_USD
    text = prompt_of(review, row["agent_run_id"])
    assert model.LENS in text and "holds 12 items" in text and "--post FILE" in text
    assert SECRET not in text and "ledger" not in text
    assert advanced(review) == {"running": 1}
    status = client.get(f"{URL}/runs", headers=owner).json()["active"]
    assert status["readable"]["items"] == 12 and "prompt" not in status


@needs_pg
def test_the_run_ends_done_when_its_review_run_posted_a_map_and_failed_otherwise(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    mapped_build(db)
    run_id = client.post(f"{URL}/run", headers=owner).json()["id"]
    advanced(review)
    agent_run = map_row(db, run_id)["agent_run_id"]

    async def post():
        async with review.client.app.state.engine.begin() as conn:
            return await kg_entity_map_db.posted(conn, agent_run)

    assert review.client.portal.call(post) == run_id
    set_run(db, agent_run, state="cancelled", finished_at=func.now())  # the map counts, not how the run ended
    assert advanced(review) == {"done": 1}
    assert map_row(db, run_id)["maps"] == 1 and map_row(db, run_id)["ended_at"] is not None
    second = client.post(f"{URL}/run", headers=owner).json()["id"]
    advanced(review)
    set_run(db, map_row(db, second)["agent_run_id"], state="cancelled", finished_at=func.now())
    assert advanced(review) == {"failed": 1}
    assert "without posting a map" in map_row(db, second)["error"]


@needs_pg
def test_a_graph_the_sink_cannot_read_fails_the_run_and_a_run_no_node_takes_fails_in_a_day(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    mapped_build(db)
    run_id = client.post(f"{URL}/run", headers=owner).json()["id"]
    assert advanced(review, count=unreadable) == {"failed": 1}
    assert "no graph on the hub yet" in map_row(db, run_id)["error"]
    run_id = client.post(f"{URL}/run", headers=owner).json()["id"]
    advanced(review)
    blocker = map_row(db, run_id)["agent_run_id"]
    t = tables.kg_entity_map_runs
    sql(db, update(t).where(t.c.id == run_id).values(state="failed", ended_at=func.now()))
    later = client.post(f"{URL}/run", headers=owner).json()["id"]  # the review run of the first still holds the slot
    assert advanced(review) == {"waiting": 1}
    assert any("another review run" in n for n in map_row(db, later)["body"]["notes"])
    tomorrow = datetime.now(UTC) + model.QUEUE_WAIT + timedelta(minutes=5)
    assert advanced(review, now=tomorrow) == {"failed": 1}
    set_run(db, blocker, state="cancelled", finished_at=func.now())


@needs_pg
def test_the_first_build_under_the_strategy_queues_a_run_and_later_builds_only_when_stale(review):
    db = review.db
    assert on_build(review, mapped_build(db, enabled=False), {"entity_map": {"enabled": False}}) is None
    build = mapped_build(db)
    first = on_build(review, build, {"entity_map": {"enabled": True, "maps": 0}})
    row = map_row(db, first)
    assert (row["via"], row["state"], row["build_id"]) == ("first_build", "queued", build)
    assert on_build(review, build, {"entity_map": {"enabled": True, "mentions": 10, "stale": 9}}) is None  # in flight
    t = tables.kg_entity_map_runs
    long_ago = datetime.now(UTC) - model.COOLDOWN - timedelta(hours=1)
    sql(db, update(t).where(t.c.id == first).values(state="done", ended_at=func.now(), created_at=long_ago))
    assert on_build(review, build, {"entity_map": {"enabled": True, "mentions": 10, "stale": 1}}) is None
    stale = on_build(review, build, {"entity_map": {"enabled": True, "mentions": 10, "stale": 9}})
    assert map_row(db, stale)["via"] == "stale" and "9 of 10 citations" in map_row(db, stale)["why"][0]


@needs_pg
def test_the_review_run_is_a_kg_run_whose_token_reaches_the_outline_and_changed_routes(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    mapped_build(db)
    run_id = client.post(f"{URL}/run", headers=owner).json()["id"]
    advanced(review)
    agent_run = map_row(db, run_id)["agent_run_id"]

    async def is_kg():
        async with review.client.app.state.engine.begin() as conn:
            return await kg_run_access.is_kg_run(conn, agent_run, "review")

    assert review.client.portal.call(is_kg)
    for path in ("outline", "changed", "runs"):
        assert kg_run_access.refusal("GET", f"{URL}/{path}", PROJECT) is None
    assert kg_run_access.refusal("POST", f"{URL}/run", PROJECT) is not None  # it starts no run
    assert kg_run_access.refusal("GET", "/v1/kg/other/entity-map/outline", PROJECT) is not None
    assert client.get(f"{URL}/outline", headers=owner).status_code == 409  # no graph artifact on the hub
