"""The route of an entity map on the hub (``evo_agents.hub.server.kg_entity_map``): only the agent of a run with the
writer role posts one, the hub checks each entry against its latest build through the run's sink, rejects what cites
a node missing or above that sink's clearance with a reason and keeps the rest as a blob, and the build of strategy
entity-graph-agent reads every map kept."""

from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import kg_entity_maps as maps
from evo_agents.hub import tables
from evo_agents.hub.blobs import blob_key
from evo_agents.hub.server import kg_run_access, security
from evo_agents.hub.server.security import RUN, Principal
from evo_agents.kg import entity_map
from tests.hub.live import sql
from tests.hub.test_kg import (
    PROJECT,
    SECRET_TEXT,
    Machine,
    call_tool,
    doc,
    jobs,
    open_hub,
    push,
    run_jobs,
    tool_ok,
)

GUIDE = "docs:doc:guide"
VAULT = "vault:doc:combo"


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        yield found
    finally:
        client.app.dependency_overrides.pop(security.principal, None)
        client.__exit__(None, None, None)


def built(hub, tmp_path, *, strategy=True):
    """A succeeded build of alpha of an internal guide and a secret vault item, its strategy entity-graph-agent."""
    if strategy:
        got = hub.client.put(
            f"/v1/kg/{PROJECT}/settings", json={"values": {"strategy": "entity-graph-agent"}}, headers=hub.alice
        )
        assert got.status_code == 200, got.text
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the search engine works."))
    laptop.sync("vault", {"key": "combo", "text": f"# Combo\n\n{SECRET_TEXT}\n", "rev": "1"})
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)


def as_run(hub, login="alice", run_id=41):
    (user,) = sql(hub.db, select(tables.users.c.id).where(tables.users.c.login == login))[0]
    hub.client.app.dependency_overrides[security.principal] = lambda: Principal(
        user, login, False, 0, RUN, "", run_id=run_id
    )
    return user


def post(hub, body, member="alice"):
    return hub.client.post(f"/v1/kg/{PROJECT}/entity-map", json=body, headers=getattr(hub, member))


MAP = {
    "format": entity_map.FORMAT,
    "model": "claude-opus-5-5",
    "run": 999,  # the hub stamps the run itself
    "entities": [
        {
            "kind": "Component",
            "name": "Search engine",
            "description": "Finds the documents.",
            "mentions": [
                {"node": GUIDE, "quote": "How the search engine works.", "line": 3},
                {"node": "docs:doc:nowhere"},
            ],
        },
        {"kind": "Concept", "name": "Combination", "mentions": [{"node": VAULT}]},
        {"kind": "Concept", "name": "Ghost", "mentions": [{"node": "docs:doc:gone"}]},
    ],
    "relations": [{"src": "Search engine", "dst": "Combination", "keywords": "x"}],
}


def test_a_run_s_map_is_checked_kept_as_a_blob_and_read_by_the_next_build(hub, tmp_path):
    built(hub, tmp_path)
    alice = as_run(hub)
    got = post(hub, MAP)
    assert got.status_code == 200, got.text
    found = got.json()
    assert (found["entities"], found["relations"], found["mentions"], found["code_links"]) == (1, 0, 1, 0)
    assert found["queued"] is True and found["sha256"]
    reasons = {r["entry"]: r["reason"] for r in found["rejected"]}
    assert set(reasons) == {
        "entities[0] (Search engine).mentions[1]",
        "entities[1] (Combination).mentions[0]",
        "entities[1] (Combination)",
        "entities[2] (Ghost).mentions[0]",
        "entities[2] (Ghost)",
        "relations[0] (Search engine - Combination)",
    }
    hidden, missing = reasons["entities[1] (Combination).mentions[0]"], reasons["entities[2] (Ghost).mentions[0]"]
    assert hidden == missing.replace("docs:doc:gone", VAULT), "a node above the clearance reads as a missing one"
    assert SECRET_TEXT not in got.text

    b = tables.blobs
    ((sha, by),) = sql(hub.db, select(b.c.sha256, b.c.created_by).where(b.c.kind == maps.MAP_KIND))
    assert sha == found["sha256"] and by == alice and hub.s3.get(blob_key(sha)) is not None
    root = tmp_path / "fetched"
    assert maps.write_maps(SimpleNamespace(fetch=hub.app.state.blobs.fetch), root, [sha]) == 1
    (kept,) = entity_map.read_maps(root)[0]
    assert kept["run"] == 41 and [e["name"] for e in kept["entities"]] == ["Search engine"]
    assert any(job[2] == f"kg:{PROJECT}" for job in jobs(hub.db)), "a build is queued to read it"

    hub.client.app.dependency_overrides.pop(security.principal, None)
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    node = tool_ok(hub, "kg_node", {"id": "entity:Component:search-engine"})["structuredContent"]
    assert node["node"]["kind"] == "Component" and node["node"]["status"] == "extracted"
    assert node["node"]["label"]["level"] == "internal", "the label of the guide it cites"
    assert node["node"]["props"]["run"] == 41
    ((coverage,),) = sql(hub.db, select(tables.kg_builds.c.coverage).order_by(tables.kg_builds.c.id.desc()).limit(1))
    assert coverage["entity_map"]["maps"] == 1 and coverage["entity_map"]["runs"] == [41]
    assert coverage["entity_map"]["nodes"] == 1 and coverage["entity_map"]["mentions"] == 1
    # carol's grant stops at public: she does not see the entity of an internal guide
    hidden_node = call_tool(hub, "kg_node", {"id": "entity:Component:search-engine"}, member="carol").json()
    assert hidden_node["isError"] and "Search engine" not in str(hidden_node)


def test_the_first_build_after_switching_to_the_strategy_queues_a_run_and_lets_a_writer_ask_for_one(hub, tmp_path):
    """kg_builds.coverage keeps entity_map.enabled: the first build under entity-graph-agent queues a run
    (``kg_entity_map_db.on_build``), and ``hub kg entity-map --run`` finds the latest build of that strategy."""
    built(hub, tmp_path, strategy=False)
    got = hub.client.put(
        f"/v1/kg/{PROJECT}/settings", json={"values": {"strategy": "entity-graph-agent"}}, headers=hub.alice
    )
    assert got.status_code == 200, got.text
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    builds = tables.kg_builds
    ((status, coverage),) = sql(
        hub.db, select(builds.c.status, builds.c.coverage).order_by(builds.c.id.desc()).limit(1)
    )
    assert status == "succeeded" and coverage["entity_map"]["enabled"] is True
    runs = tables.kg_entity_map_runs
    assert [r[0] for r in sql(hub.db, select(runs.c.via))] == ["first_build"]
    sql(hub.db, update(runs).values(state="done"))  # the queued run ended: a writer's call starts a new one
    asked = hub.client.post(f"/v1/kg/{PROJECT}/entity-map/run", headers=hub.alice)
    assert asked.status_code == 201, asked.text


def test_only_a_run_with_the_writer_role_posts_a_map_and_a_wrong_one_keeps_nothing(hub, tmp_path):
    built(hub, tmp_path, strategy=False)
    refused = post(hub, MAP)
    assert refused.status_code == 403 and "run of the project" in refused.text
    as_run(hub, "carol", 42)
    assert post(hub, MAP, member="carol").status_code == 403, "a reader's run may not"
    as_run(hub)
    assert post(hub, {**MAP, "format": "evo-entity-map/0"}).status_code == 422
    nothing = post(hub, {**MAP, "entities": [MAP["entities"][2]], "relations": []})
    assert nothing.status_code == 200, nothing.text
    assert nothing.json()["sha256"] is None and nothing.json()["queued"] is False
    assert sql(hub.db, select(tables.blobs.c.sha256).where(tables.blobs.c.kind == maps.MAP_KIND)) == []
    assert not [job for job in jobs(hub.db) if job[1] == "todo"]


def test_a_project_without_a_build_cannot_check_a_map(hub):
    as_run(hub)
    got = post(hub, MAP)
    assert got.status_code == 409 and "no knowledge graph" in got.text


def test_a_kg_run_s_token_reaches_the_route():
    assert kg_run_access.refusal("POST", f"/v1/kg/{PROJECT}/entity-map", PROJECT) is None
    assert kg_run_access.refusal("POST", "/v1/kg/other/entity-map", PROJECT) is not None
