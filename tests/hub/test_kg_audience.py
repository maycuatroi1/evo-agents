"""Every read of a project's knowledge graph on the hub holds the reader's audience, computed from the database at that
read (``evo_agents.hub.server.kg_audience``), as criterion a3 of plan chat-history-in-kg names it.

Checked through the kg_* tools of REST and /mcp, kg_more, and the web's graph routes: a member of the project in no chat
sees no node, edge, count or search result of one; a member who joined a chat after the agent of another project left
reads its later messages, not those that need a grant on that project; a member taken out of a chat, or whose grant no
longer clears its label, loses it at the next read, with no build, and kg_more no longer continues a result that named
it; what is derived from messages of two chats shows only to who is in both; the agent of a chat run that went to the
worker of another member reads what both people hold; a run's agent of another kind reads as its owner.

The graph is built on a machine from items that carry the audiences the chats of this hub give (the connector of
``tests.kg.test_audience``) and installed as the project's latest build, in the api's cache, as ``tests.hub.test_mcp``
installs its graph. Project aud has the graph; project aud-b only has an agent that was in chat one and is in chat two.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, literal, select

from evo_agents.hub import tables
from evo_agents.hub.access import ProjectRules
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.kg_audience import person_audience, reader_audience
from evo_agents.hub.server.security import Principal, RunScope
from evo_agents.kg import serve
from evo_agents.kg.policy import Label
from tests.hub.kg_fixture import AGENT, LEVELS
from tests.hub.test_mcp import MCP_HEADERS, call

A, B = "aud", "aud-b"
PLAN, M1, M2, M3 = "chat:text:plan", "chat:text:m1", "chat:text:m2", "chat:text:m3"
K7, K9 = "code:K-7", "code:K-9"
GB = f"grant:{B}:internal"
GRANTS = {  # login: {project: (role, max level)}
    "alice": {A: ("writer", "internal"), B: ("writer", "internal")},  # made both chats, is in both
    "dave": {A: ("reader", "internal"), B: ("reader", "internal")},  # in both chats from the start
    "bob": {A: ("reader", "internal")},  # joined chat one once the agent of aud-b had left
    "carol": {A: ("reader", "internal")},  # in no chat
}
KNOWLEDGE = """\
version: 1
project: aud
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {{id: claude-code@anthropic, kind: agent-session, clearance: {{level: customer}}}}
    - {{id: hub, kind: hub, clearance: {{level: customer}}}}
sources:
  - id: chat
    connector: "python:tests.kg.test_audience:connector"
    config: {{file: '{file}'}}
    label: {{level: internal, integrity: U}}
"""


def registration(name: str) -> dict:
    return {
        "levels": LEVELS,
        "locations": ["any"],
        "default_label": {"level": "internal"},
        "sinks": [
            {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
            {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
        ],
        "repos": [{"name": "app", "path": "app", "origin": f"https://git.example.org/{name}/app.git"}],
        "harness": {"name": name, "workspace": "~/ws", "path": f"{name}-harness"},
    }


def items(one: int, two: int) -> list[dict]:
    """The chat items of the graph: a plan without an audience, and messages of chats ``one`` and ``two``."""

    def said(src: str, node: str, mentioned: str) -> dict:
        return {
            "nodes": [{"id": node, "kind": "Code", "name": node.removeprefix("code:")}],
            "edges": [{"src": src, "rel": "mentions", "dst": mentioned}],
        }

    first, second = f"chat:{one}", f"chat:{two}"
    return [
        {"key": "plan", "title": "Release plan", "text": "Release plan for the search service."},
        {
            "key": "m1",
            "title": "#1 alice",
            "text": "The kiwi rollout breaks the search service.",
            "audience": sorted([first, GB]),  # sent while the agent of aud-b was in chat one
            "graph": said(M1, K9, PLAN),
        },
        {
            "key": "m2",
            "title": "#2 dave",
            "text": "Kiwi rollout fixed after the search service restart.",
            "audience": [first],
            "graph": said(M2, K7, PLAN),
        },
        {
            "key": "m3",
            "title": "#3 alice",
            "text": "Mango picks up what chat one said.",
            "audience": sorted([second, GB]),
            "graph": said(M3, K7, M2),
        },
    ]


def build(root: Path, monkeypatch, one: int, two: int) -> SimpleNamespace:
    """The graph of aud with the items of chats ``one`` and ``two``, built on a machine."""
    from evo_agents.hub.kg_build import build_graph
    from evo_agents.kg.project import load_project_at
    from evo_agents.kg.sync import sync_project

    harness, home, spec = root / f"{A}-harness", root / "kg-home", root / "chat.json"
    harness.mkdir(parents=True)
    spec.write_text(json.dumps({"items": items(one, two)}))
    (harness / "harness.yaml").write_text(f"name: {A}\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (harness / "knowledge.yaml").write_text(KNOWLEDGE.format(file=spec))
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    project = load_project_at(harness, home)
    assert all(result.ok for result in sync_project(project, ["chat"]))
    path, report = build_graph(project)
    data = path.read_bytes()
    return SimpleNamespace(data=data, sha256=hashlib.sha256(data).hexdigest(), report=report)


def install(db, cache: Path, graph: SimpleNamespace) -> None:
    """``graph`` as the latest successful build of aud, in the api's cache already."""
    target = cache / "kg" / "graphs" / A / f"{graph.sha256}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(graph.data)
    report, projects = graph.report, tables.projects
    found = select(
        projects.c.id,
        literal("succeeded"),
        literal(graph.sha256),
        literal(len(graph.data)),
        literal(report.content_hash),
        literal(report.nodes),
        literal(report.edges),
        func.now(),
        func.now(),
    ).where(projects.c.name == A)
    columns = ["project_id", "status", "artifact_sha256", "artifact_size", "content_hash", "nodes", "edges"]
    live.sql(db, insert(tables.kg_builds).from_select([*columns, "started_at", "finished_at"], found))


def done(response, status: int = 200) -> dict | None:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@pytest.fixture
def hub(hub_db, tmp_path, monkeypatch):
    """Projects aud and aud-b; the members of GRANTS; chat one, made by alice with dave and the agents of both
    projects, which the agent of aud-b then left and bob joined; chat two, of alice and dave with both agents still;
    and the graph of aud with messages of both chats."""
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
        for name in (A, B):
            done(client.put(f"/v1/projects/{name}", json=registration(name), headers=admin))
        members = {}
        for login, grants in GRANTS.items():
            for project, (role, level) in grants.items():
                body = {"role": role, "max_level": level}
                done(client.put(f"/v1/admin/projects/{project}/grants/{login}", json=body, headers=admin))
            members[login] = live.bearer(live.insert_token(hub_db, login))
        chats = []
        for title in ("Kiwi rollout", "Mango"):
            body = {"title": title, "people": ["alice", "dave"], "agents": [A, B]}
            chats.append(done(client.post("/v1/chats", json=body, headers=members["alice"]), 201)["id"])
        one, two = chats
        done(client.delete(f"/v1/chats/{one}/agents/{B}", headers=members["alice"]), 204)
        done(client.put(f"/v1/chats/{one}/people/bob", headers=members["alice"]))
        graph = build(tmp_path / "laptop", monkeypatch, one, two)
        install(hub_db, tmp_path / "cache", graph)
        yield SimpleNamespace(client=client, db=hub_db, admin=admin, one=one, two=two, graph=graph, **members)


def rest(hub, login: str, tool: str, **arguments) -> dict:
    """A kg_* tool through POST /v1/kg/aud/tools/{tool}, with the structured result."""
    body = {"arguments": arguments, "sink": AGENT}
    return done(hub.client.post(f"/v1/kg/{A}/tools/{tool}", json=body, headers=getattr(hub, login)))


def mcp(hub, login: str, tool: str, **arguments) -> dict:
    """A kg_* tool through /mcp in a session of aud, with the structured result."""
    headers = {**getattr(hub, login), **MCP_HEADERS, "X-Evo-Project": A}
    return done(hub.client.post("/mcp", json=call(7, tool, arguments, data=True), headers=headers))["result"]


def web(hub, login: str, path: str, status: int = 200, **params) -> dict | None:
    return done(hub.client.get(f"/v1/kg/{A}/{path}", params=params, headers=getattr(hub, login)), status)


def data(result: dict) -> dict:
    assert not result.get("isError"), result
    return result["structuredContent"]


def found(result: dict) -> set[str]:
    return {n["id"] for n in data(result)["results"]}


def seen(result: dict) -> bool:
    return not result.get("isError")


def mentions(node: dict, way: str) -> set[str]:
    return {e["node"] for e in node[way] if e["rel"] == "mentions"}


def user_id(hub, login: str) -> int:
    return live.sql(hub.db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def on_hub(hub, work):
    """``work(conn)`` in one transaction of the hub's engine, on its event loop."""

    async def run(engine):
        async with engine.begin() as conn:
            return await work(conn)

    return hub.client.portal.call(partial(run, hub.client.app.state.engine))


# Reading


def test_a_member_in_no_chat_sees_no_node_edge_count_or_search_result_of_one(hub):
    for read in (rest, mcp):
        assert found(read(hub, "carol", "kg_search", query="kiwi rollout")) == set()
        assert found(read(hub, "carol", "kg_search", query="search service")) == {PLAN}
        assert not any(seen(read(hub, "carol", "kg_node", id=node)) for node in (M1, M2, M3, K7, K9))
        assert mentions(data(read(hub, "carol", "kg_node", id=PLAN)), "in") == set()
        assert data(read(hub, "carol", "kg_impact", id=PLAN))["documents"] == []
    assert web(hub, "carol", "nodes", q="kiwi rollout")["results"] == []
    assert web(hub, "carol", "node", 404, id=M2) is not None
    around = web(hub, "carol", "neighbourhood", id=PLAN)
    assert {n["id"] for n in around["nodes"]} == {PLAN, "source:chat"} and around["neighbours"] == 1
    # Counts: the kinds, the build's nodes and edges, and kg_status leave out what carol's audience lacks.
    mine, whole = web(hub, "carol", "graph"), web(hub, "alice", "graph")
    assert "Code" not in {k["kind"] for k in mine["kinds"]} and "Code" in {k["kind"] for k in whole["kinds"]}
    assert mine["graph"]["nodes"] == sum(k["count"] for k in mine["kinds"]) == 2  # the plan and its source
    assert (whole["graph"]["nodes"], whole["graph"]["edges"]) == (hub.graph.report.nodes, hub.graph.report.edges)
    assert mine["graph"]["edges"] < whole["graph"]["edges"]
    # The shape counts an edge only when carol sees it and both its nodes: the plan in its source, and no more.
    shape = web(hub, "carol", "shape")["shape"]
    assert shape == [{"src_kind": "Document", "rel": "in_source", "dst_kind": "Source", "count": 1}]
    assert sum(r["count"] for r in web(hub, "alice", "shape")["shape"]) > sum(r["count"] for r in shape)
    grouped = web(hub, "carol", "neighbourhood", id=PLAN, group=5)
    assert [g["shown"] for g in grouped["groups"]] == [["source:chat"]]
    assert {n["id"]: n["degree"] for n in grouped["nodes"]}[PLAN] == 1
    status = rest(hub, "carol", "kg_status")
    graph = data(status)["graph"]
    assert (graph["nodes"], graph["edges"]) == (mine["graph"]["nodes"], mine["graph"]["edges"])
    assert graph["coverage"] == {"sources": [{"id": "chat", "items": 1}], "warnings": []}
    assert "top_audience" not in graph["stats"] and "size" not in graph["stats"]
    assert f"{'chat':<22} {1:>6} items you can read" in status["content"][0]["text"]
    full = data(rest(hub, "alice", "kg_status"))["graph"]
    assert full["nodes"] == hub.graph.report.nodes and full["coverage"]["sources"][0]["items"] == 4
    assert full["stats"]["top_audience"] == sorted([f"chat:{hub.one}", f"chat:{hub.two}", GB])


def test_a_member_who_joined_later_reads_what_they_clear_and_not_what_needs_a_grant_they_lack(hub):
    for read in (rest, mcp):
        assert found(read(hub, "bob", "kg_search", query="kiwi rollout")) == {M2, PLAN}
        assert found(read(hub, "dave", "kg_search", query="kiwi rollout")) == {M1, M2, PLAN}
        assert not seen(read(hub, "bob", "kg_node", id=M1)) and not seen(read(hub, "bob", "kg_node", id=M3))
        node = data(read(hub, "bob", "kg_node", id=M2))
        assert node["node"]["label"]["audience"] == [f"chat:{hub.one}"]
        assert mentions(data(read(hub, "bob", "kg_node", id=PLAN)), "in") == {M2}
    assert {n["id"] for n in web(hub, "bob", "nodes", q="kiwi rollout")["results"]} == {M2, PLAN}
    assert web(hub, "bob", "node", 404, id=M1) is not None
    assert web(hub, "bob", "node", id=M2)["node"]["id"] == M2
    assert {n["id"] for n in web(hub, "bob", "neighbourhood", id=PLAN)["nodes"]} == {PLAN, "source:chat", M2}


def test_what_is_derived_from_two_chats_shows_only_to_who_is_in_both(hub):
    both = sorted([f"chat:{hub.one}", f"chat:{hub.two}", GB])
    for read in (rest, mcp):
        assert data(read(hub, "alice", "kg_node", id=K7))["node"]["label"]["audience"] == both
        assert seen(read(hub, "dave", "kg_node", id=K7)) and not seen(read(hub, "bob", "kg_node", id=K7))
        assert mentions(data(read(hub, "alice", "kg_node", id=M3)), "out") == {M2}
        assert mentions(data(read(hub, "bob", "kg_node", id=M2)), "in") == set()  # m3 is of chat two
        assert data(read(hub, "alice", "kg_path", **{"from": M3, "to": PLAN}))["found"]
    assert web(hub, "bob", "node", 404, id=K7) is not None
    assert "Code" not in {k["kind"] for k in web(hub, "bob", "graph")["kinds"]}  # K-9 is of m1 alone
    assert {k["kind"]: k["count"] for k in web(hub, "alice", "graph")["kinds"]}["Code"] == 2


# Changes of members and grants count at the next read


def test_a_member_taken_out_of_a_chat_loses_it_at_the_next_read_and_kg_more_stops(hub, monkeypatch):
    monkeypatch.setattr(serve, "CAP_CHARS", 200)  # every answer is cut, so kg_more has something to continue
    cut = rest(hub, "dave", "kg_search", query="kiwi rollout")["structuredContent"]
    assert cut["truncated"] and set(cut["label"]["audience"]) == {f"chat:{hub.one}", GB}
    assert seen(mcp(hub, "dave", "kg_more", handle=cut["handle"]))  # in the chat: the rest comes
    monkeypatch.undo()
    built = data(rest(hub, "dave", "kg_search", query="kiwi rollout"))["build"]
    done(hub.client.delete(f"/v1/chats/{hub.one}/people/dave", headers=hub.alice), 204)
    after = data(rest(hub, "dave", "kg_search", query="kiwi rollout"))
    assert after["build"] == built and after["results"] == []  # no build in between
    assert not seen(mcp(hub, "dave", "kg_node", id=M2)) and web(hub, "dave", "node", 404, id=M1) is not None
    assert seen(rest(hub, "dave", "kg_node", id=M3))  # still in chat two
    refused = mcp(hub, "dave", "kg_more", handle=cut["handle"])
    assert refused["isError"] and "may no longer read" in refused["content"][0]["text"]


def test_a_member_whose_grant_no_longer_clears_a_chat_loses_it_at_the_next_read(hub):
    dave = user_id(hub, "dave")
    assert on_hub(hub, lambda conn: person_audience(conn, dave)) == {
        f"chat:{hub.one}",
        f"chat:{hub.two}",
        f"grant:{A}:public",
        f"grant:{A}:internal",
        f"grant:{B}:public",
        GB,
    }
    assert seen(rest(hub, "dave", "kg_node", id=M3)) and seen(rest(hub, "dave", "kg_node", id=M1))
    done(hub.client.delete(f"/v1/admin/projects/{B}/grants/dave", headers=hub.admin), 204)
    # Still a member of chat two, whose agent of aud-b he no longer clears; chat one has the agent of aud alone.
    assert on_hub(hub, lambda conn: person_audience(conn, dave)) == {
        f"chat:{hub.one}",
        f"grant:{A}:public",
        f"grant:{A}:internal",
    }
    for read in (rest, mcp):
        assert not seen(read(hub, "dave", "kg_node", id=M3)) and not seen(read(hub, "dave", "kg_node", id=M1))
        assert found(read(hub, "dave", "kg_search", query="kiwi rollout")) == {M2, PLAN}
    body = {"role": "reader", "max_level": "public"}  # a grant below the label of aud-b clears no more
    done(hub.client.put(f"/v1/admin/projects/{B}/grants/dave", json=body, headers=hub.admin))
    assert on_hub(hub, lambda conn: person_audience(conn, dave)) == {
        f"chat:{hub.one}",
        f"grant:{A}:public",
        f"grant:{A}:internal",
        f"grant:{B}:public",
    }


# The agents of runs


def worker(hub, login: str, name: str) -> SimpleNamespace:
    """A worker of ``login`` serving aud, after a first heartbeat."""
    host = {"hostname": f"{name}.local", "os": "darwin", "arch": "arm64", "agent_version": "0.4.0"}
    body = {"name": name, "projects": [A], "slots": 2, **host}
    made = done(hub.client.post("/v1/workers", json=body, headers=getattr(hub, login)), 201)
    found = SimpleNamespace(
        id=made["worker"]["id"],
        token=made["token"],
        headers={**live.bearer(made["token"]), "X-Evo-Worker-Protocol": "1"},
    )
    beat = {
        "runtimes": {"claude-code": {"available": True, "version": "2.1.289"}},
        "checkouts": {f"{A}/app": {"path": "/src/app", "branch": "main"}},
        "free_slots": 2,
    }
    done(hub.client.post("/v1/worker/heartbeat", json=beat, headers=found.headers))
    return found


def chat_run(hub, held_by: SimpleNamespace, owner: str, message_id: int) -> int:
    """A running chat run of the default agent of aud on ``held_by``, a worker of ``owner``, started by message
    ``message_id`` of chat one: run_as when its author is someone else."""
    runs, profiles = tables.runs, tables.agent_profiles
    profile = select(profiles.c.id).join_from(profiles, tables.projects, tables.projects.c.id == profiles.c.project_id)
    profile = profile.where(tables.projects.c.name == A, profiles.c.is_default).scalar_subquery()
    project = select(tables.projects.c.id).where(tables.projects.c.name == A).scalar_subquery()
    now = func.now()
    row = {
        "project_id": project,
        "profile_id": profile,
        "profile_version": 1,
        "dispatched_by": user_id(hub, owner),
        "kind": "chat",
        "branch": "chat/1",
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": 3600,
        "state": "running",
        "worker_id": held_by.id,
        "chat_id": hub.one,
        "chat_message_id": message_id,
        "hop": 1,
        "leased_at": now,
        "lease_expires_at": now + timedelta(minutes=5),
        "started_at": now,
    }
    return live.sql(hub.db, insert(runs).values(row).returning(runs.c.id))[0][0]


def agent(hub, held_by: SimpleNamespace, run_id: int, tool: str, **arguments) -> dict:
    """A kg_* tool from the agent of run ``run_id``: its worker's token and X-Evo-Run, as `evo-agents hub mcp` sends."""
    headers = {**live.bearer(held_by.token), **MCP_HEADERS, "X-Evo-Run": str(run_id)}
    return done(hub.client.post("/mcp", json=call(7, tool, arguments, data=True), headers=headers))["result"]


def test_the_agent_of_a_chat_run_on_another_members_worker_reads_what_both_people_hold(hub):
    asked = done(hub.client.post(f"/v1/chats/{hub.one}/messages", json={"body": "kiwi?"}, headers=hub.bob), 201)
    studio = worker(hub, "alice", "studio")
    run_id = chat_run(hub, studio, "alice", asked["id"])  # bob asked; it runs on alice's worker
    assert found(mcp(hub, "alice", "kg_search", query="kiwi rollout")) == {M1, M2, PLAN}
    assert found(agent(hub, studio, run_id, "kg_search", query="kiwi rollout")) == {M2, PLAN}
    for hidden in (M1, M3, K7):  # m1 needs a grant on aud-b, m3 and K-7 chat two: bob holds neither
        assert not seen(agent(hub, studio, run_id, "kg_node", id=hidden))
    principal = Principal(
        user_id(hub, "alice"), "alice", False, 0, "worker", "", RunScope(run_id, A, "writer", "internal", "chat")
    )
    both = on_hub(hub, lambda conn: reader_audience(conn, principal))
    assert both == {f"chat:{hub.one}", f"grant:{A}:public", f"grant:{A}:internal"}
    # The agent of a run of another kind reads as its owner, the person its worker belongs to.
    step = Principal(principal.user_id, "alice", False, 0, "worker", "", RunScope(run_id, A, "writer", "internal"))
    alone = on_hub(hub, lambda conn: reader_audience(conn, step))
    assert alone == on_hub(hub, lambda conn: person_audience(conn, principal.user_id)) and GB in alone


def test_a_sinks_clearance_keeps_the_readers_audience_and_takes_a_push_with_one():
    rules = ProjectRules(A, LEVELS, ["any"], registration(A)["sinks"], {"level": "internal"})
    audience = frozenset({"chat:5", GB})
    assert rules.ceiling("internal", AGENT, audience).audience == audience
    assert rules.ceiling("internal", AGENT).audience == frozenset()
    assert rules.grant_label("internal", audience).audience == audience
    cleared = rules.clearance_label(rules.hub_sink)
    assert Label(1, 0, "U", frozenset({A}), audience).below(cleared)  # an item with an audience may be pushed
