"""The read-only knowledge graph routes of the hub's web pages (``evo_agents.hub.server.kg_web``).

Checked here: search, node and neighbourhood answer from the latest successful build with the build's content hash;
a member whose grant stops at internal never receives a customer node or edge, by search, by id (404, as for a node
that does not exist), in a neighbourhood or in the counts by kind; the web reads with the grant alone while the kg_*
tools read with the grant met with the sink, and the two agree whenever the sink clears the grant; the neighbours the
graph view shows one step out are those kg_context returns on the same store; the neighbourhood stops at 2 steps and
150 nodes and says how many it left out; grouped, it shows the node's direct neighbours by edge type, direction and
kind, the most connected first, a few of each group unless the group is opened; the graph's shape counts visible
edges by source kind, edge type and target kind, and only those; the graph summary and kg_status name the commit each source read, the
summary only for the sources the member can see; the build list the status page reads carries content hash, counts,
times, errors and the jobs still queued; 401, 403, 404 and a project without a build.

One hub serves every test of the module (they only read): the fixture of ``tests.hub.kg_fixture`` pushed by a writer
through ``evo_agents.hub.kg_push`` and built by the worker's job in this process, the blob store being moto's server
(``tests.hub.s3``). Nothing touches ~/.evo/kg.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.hub import live, pg
from tests.hub.kg_fixture import (
    AGENT,
    CONTRACT,
    CONTRACT_SECTION,
    DEALS_SHA,
    DOCS_READ,
    DOCS_SHA,
    HUB_NODE,
    NARROW,
    NOTES,
    RUNBOOK,
    RUNBOOK_SECTION,
    SHARED_NODE,
    Harness,
    registration,
)

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub.server.app import create_app
from tests.hub.s3 import fake_s3
from tests.hub.test_kg import InProcessHub, make_config, run_jobs

PROJECT = "kgweb"
EMPTY = "kgweb-empty"  # registered, never pushed: no graph
MEMBERS = {
    "wendy": ("writer", "customer"),  # pushed the fixture
    "ivan": ("reader", "internal"),
    "pat": ("reader", "public"),
}
LOW_LEVELS = {"public", "internal"}
HUB_DEGREE_SEEN = 150  # the source of every document has more visible edges than this


def _grant(client, admin: dict, project: str, login: str, role: str, level: str) -> None:
    response = client.put(
        f"/v1/admin/projects/{project}/grants/{login}", json={"role": role, "max_level": level}, headers=admin
    )
    assert response.status_code in (200, 201), response.text


@pytest.fixture(scope="module")
def hub(pg_server, tmp_path_factory):
    base = tmp_path_factory.mktemp("kg-api")
    db = pg.create_database()
    try:
        with fake_s3() as s3:
            app = create_app(make_config(db, base / "api", s3))
            with TestClient(app) as client:
                admin = live.bearer(live.insert_token(db, live.ADMIN))
                for name in (PROJECT, EMPTY):
                    response = client.put(f"/v1/projects/{name}", json=registration(name), headers=admin)
                    assert response.status_code == 200, response.text
                members = {}
                for login, (role, level) in MEMBERS.items():
                    for name in (PROJECT, EMPTY):
                        _grant(client, admin, name, login, role, level)
                    members[login] = live.bearer(live.insert_token(db, login))
                harness = Harness(base / "machine", PROJECT)
                harness.sync()
                report = harness.push(InProcessHub(client, members["wendy"]))
                assert len(report.pushed) == 2 and report.build["status"] == "queued"
                run_jobs(db, s3, base / "worker")
                builds = client.get(f"/v1/kg/{PROJECT}/builds", headers=members["wendy"]).json()["builds"]
                assert builds[0]["status"] == "succeeded", builds[0]
                yield SimpleNamespace(client=client, db=db, admin=admin, build=builds[0], harness=harness, **members)
        assert pg.wait_no_backends(db.name) == 0
    finally:
        pg.drop_database(db)


def get(hub, member: str, path: str, **params):
    headers = getattr(hub, member) if isinstance(member, str) else member
    return hub.client.get(path, params=params, headers=headers)


def ok(hub, member: str, path: str, **params) -> dict:
    response = get(hub, member, path, **params)
    assert response.status_code == 200, response.text
    return response.json()


def tool(hub, member: str, name: str, arguments: dict, sink: str = AGENT) -> dict:
    response = hub.client.post(
        f"/v1/kg/{PROJECT}/tools/{name}", json={"arguments": arguments, "sink": sink}, headers=getattr(hub, member)
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result.get("isError"), result
    return result["structuredContent"]


def levels_of(*nodes) -> set[str]:
    return {node["label"]["level"] for node in nodes}


# Reading


def test_the_graph_summary_names_the_build_and_counts_visible_nodes_by_kind(hub):
    summary = ok(hub, "wendy", f"/v1/kg/{PROJECT}/graph")
    graph = summary["graph"]
    assert graph["build_id"] == hub.build["id"] and graph["content_hash"] == hub.build["content_hash"]
    assert (graph["nodes"], graph["edges"], graph["latest"]) == (hub.build["nodes"], hub.build["edges"], True)
    kinds = {k["kind"]: k["count"] for k in summary["kinds"]}
    assert kinds["Document"] == 2 + NOTES + 1 and kinds["Requirement"] == 4
    counts = [k["count"] for k in summary["kinds"]]
    assert counts == sorted(counts, reverse=True)

    internal = {k["kind"]: k["count"] for k in ok(hub, "ivan", f"/v1/kg/{PROJECT}/graph")["kinds"]}
    assert internal["Document"] == 2 + NOTES  # the contract is customer
    assert sum(internal.values()) < sum(kinds.values())
    assert ok(hub, "pat", f"/v1/kg/{PROJECT}/graph")["kinds"] == []  # everything is internal or above


def test_the_graph_summary_names_the_commit_each_source_the_member_can_see_read(hub):
    docs = {"source": "docs", **DOCS_READ}
    deals = {"source": "deals", "commit": DEALS_SHA, "ref": "HEAD", "base": None, "behind": None, "url": None}
    assert ok(hub, "wendy", f"/v1/kg/{PROJECT}/graph")["sources"] == [deals, docs]
    assert ok(hub, "ivan", f"/v1/kg/{PROJECT}/graph")["sources"] == [docs]  # deals is customer
    assert ok(hub, "pat", f"/v1/kg/{PROJECT}/graph")["sources"] == []

    response = hub.client.post(
        f"/v1/kg/{PROJECT}/tools/kg_status", json={"arguments": {}, "sink": AGENT}, headers=hub.wendy
    )
    assert response.status_code == 200, response.text
    status = response.json()
    text = status["content"][0]["text"]
    assert f"read {DOCS_SHA[:7]} of origin/main, 2 behind origin/main" in text
    assert f"read {DEALS_SHA[:7]} of HEAD" in text
    assert status["structuredContent"]["reads"] == [deals, docs]


def test_search_finds_the_fixture_node_as_kg_search_does(hub):
    found = ok(hub, "wendy", f"/v1/kg/{PROJECT}/nodes", q="runbook")
    assert found["graph"]["content_hash"] == hub.build["content_hash"]
    assert found["query"] == "runbook" and found["kinds"] == []
    ids = [node["id"] for node in found["results"]]
    assert ids[0] == RUNBOOK
    runbook = found["results"][0]
    assert runbook["kind"] == "Document" and runbook["label"] == {
        "level": "internal",
        "location": "any",
        "integrity": "U",
    }
    assert runbook["props"]["uri"] == "https://example.test/runbook"
    # the same function as the tool: through a sink that clears the grant, the same nodes in the same order
    assert [n["id"] for n in tool(hub, "wendy", "kg_search", {"query": "runbook", "limit": 25})["results"]] == ids

    sections = ok(hub, "wendy", f"/v1/kg/{PROJECT}/nodes", q="KB-01", kind=["Section"])
    assert sections["kinds"] == ["Section"] and sections["results"]
    assert {node["kind"] for node in sections["results"]} == {"Section"}
    assert len(ok(hub, "wendy", f"/v1/kg/{PROJECT}/nodes", q="note", limit=3)["results"]) == 3


PARITY_QUERIES = ("runbook", "KB-01", "note", "acme-contract", "Hợp đồng", "deploy the hub", "nothing-matches-this")


def test_search_parity_a_local_store_and_the_hub_api_rank_the_same_nodes_in_the_same_order(hub):
    """a3, code part: the same questions asked of the same corpus built on this machine and on the hub."""
    from evo_agents.kg import evaluate as ev
    from evo_agents.kg.build import build_project
    from evo_agents.kg.serve import Session

    project = hub.harness.project()
    local = build_project(project)
    assert local.ok and local.content_hash == hub.build["content_hash"], "the same corpus builds the same graph"
    session = Session(project, AGENT)
    for query in PARITY_QUERIES:
        for limit in (5, 25):
            arguments = {"query": query, "limit": limit}
            mine = session.call("kg_search", arguments)
            assert not mine.get("isError"), mine
            hubs = tool(hub, "wendy", "kg_search", arguments)
            assert [n["id"] for n in mine["structuredContent"]["results"]] == [n["id"] for n in hubs["results"]], (
                query,
                limit,
            )

    class HubTool:
        def call(self, name, arguments):
            response = hub.client.post(
                f"/v1/kg/{PROJECT}/tools/{name}", json={"arguments": arguments, "sink": AGENT}, headers=hub.wendy
            )
            assert response.status_code == 200, response.text
            return response.json()

    questions = ev.parse_questions(
        [
            f'{{"id": "{n}", "question": "{query}", "type": "words", "nodes": ["{RUNBOOK}"]}}'
            for n, query in enumerate(PARITY_QUERIES)
        ]
    )
    ranks = {}
    for name, asked in (("local", session), ("hub", HubTool())):
        report = ev.evaluate(
            questions, session_for=lambda _, s=asked: s, checkouts_for=lambda _: {}, methods=["kg_search"]
        )
        ranks[name] = [(o.question.id, o.ranks, o.count) for o in report.outcomes]
    assert ranks["local"] == ranks["hub"] and any(r[1] == [1] for r in ranks["hub"])


def test_a_reader_whose_grant_stops_at_internal_never_receives_customer_nodes(hub):
    # the writer whose grant reaches customer sees the contract: the fixture holds it
    assert ok(hub, "wendy", f"/v1/kg/{PROJECT}/nodes", q="acme-contract")["results"][0]["id"] == CONTRACT
    assert ok(hub, "wendy", f"/v1/kg/{PROJECT}/node", id=CONTRACT)["node"]["label"]["level"] == "customer"

    for query in ("acme-contract", "Acme", "Hợp đồng", CONTRACT):
        found = ok(hub, "ivan", f"/v1/kg/{PROJECT}/nodes", q=query)
        assert levels_of(*found["results"]) <= LOW_LEVELS
        assert not any(node["id"].startswith("deals:") for node in found["results"]), query

    hidden = get(hub, "ivan", f"/v1/kg/{PROJECT}/node", id=CONTRACT)
    missing = get(hub, "ivan", f"/v1/kg/{PROJECT}/node", id="deals:doc:no-such-contract")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"] == "not_found"
    assert hidden.json()["message"] == missing.json()["message"]  # a hidden node looks exactly like a missing one
    assert get(hub, "ivan", f"/v1/kg/{PROJECT}/neighbourhood", id=CONTRACT).status_code == 404
    assert get(hub, "ivan", f"/v1/kg/{PROJECT}/node", id=CONTRACT_SECTION).status_code == 404

    # KB-01 is mentioned by the runbook and by the contract: the reader gets the runbook side only
    shared = ok(hub, "ivan", f"/v1/kg/{PROJECT}/node", id=SHARED_NODE)
    neighbours = [r["node"] for r in shared["incoming"] + shared["outgoing"]]
    assert neighbours and not any(n.startswith("deals:") for n in neighbours)
    assert levels_of(shared["node"]) <= LOW_LEVELS
    assert not any(ev["item"].startswith("deals:") for ev in shared["evidence"])
    around = ok(hub, "ivan", f"/v1/kg/{PROJECT}/neighbourhood", id=SHARED_NODE)
    assert levels_of(*around["nodes"]) <= LOW_LEVELS
    assert not any("deals" in part for e in around["edges"] for part in (e["src"], e["dst"]))
    writer = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=SHARED_NODE)
    assert any(node["id"].startswith(CONTRACT) for node in writer["nodes"])
    assert around["neighbours"] == writer["neighbours"] - 1


def test_a_reader_below_every_label_sees_an_empty_graph(hub):
    assert ok(hub, "pat", f"/v1/kg/{PROJECT}/nodes", q="runbook")["results"] == []
    assert get(hub, "pat", f"/v1/kg/{PROJECT}/node", id=RUNBOOK).status_code == 404


def test_the_web_reads_with_the_grant_while_the_tools_meet_it_with_the_sink(hub):
    web = ok(hub, "wendy", f"/v1/kg/{PROJECT}/nodes", q="acme-contract")["results"]
    assert web and web[0]["id"] == CONTRACT
    narrow = tool(hub, "wendy", "kg_search", {"query": "acme-contract"}, sink=NARROW)["results"]
    assert not any(node["id"].startswith("deals:") for node in narrow)  # the sink clears internal only
    wide = tool(hub, "wendy", "kg_search", {"query": "acme-contract"})["results"]
    assert [n["id"] for n in wide] == [n["id"] for n in web]
    # the reader's grant is the lower bound: the agent sink clears customer, the meet is internal, as on the web
    for member in ("ivan", "wendy"):
        through_agent = tool(hub, member, "kg_node", {"id": SHARED_NODE})
        on_web = ok(hub, member, f"/v1/kg/{PROJECT}/node", id=SHARED_NODE)
        assert on_web["outgoing"] == through_agent["out"] and on_web["incoming"] == through_agent["in"]
        assert on_web["evidence"] == through_agent["evidence"]


@pytest.mark.parametrize("node_id", [RUNBOOK, SHARED_NODE, RUNBOOK_SECTION])
@pytest.mark.parametrize("member", ["wendy", "ivan"])
def test_the_neighbours_one_step_out_are_those_kg_context_returns(hub, node_id, member):
    around = ok(hub, member, f"/v1/kg/{PROJECT}/neighbourhood", id=node_id, hops=1)
    context = tool(hub, member, "kg_context", {"ids": [around["focus"]], "hops": 1, "budget_tokens": 8000})
    assert around["focus"] == node_id
    assert {n["id"] for n in around["nodes"]} == {n["id"] for n in context["nodes"]}
    assert {(e["src"], e["rel"], e["dst"]) for e in around["edges"]} == {
        (e["src"], e["rel"], e["dst"]) for e in context["edges"]
    }
    assert around["neighbours"] == len(around["nodes"]) - 1 and not around["truncated"]
    assert {n["hop"] for n in around["nodes"]} <= {0, 1}


def test_the_neighbourhood_stops_at_two_steps_and_150_nodes_and_says_it_cut(hub):
    around = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=HUB_NODE)
    assert (around["hops"], around["limit"]) == (2, 150)
    assert len(around["nodes"]) == 150 and around["truncated"]
    assert around["neighbours"] == NOTES and sum(n["hop"] == 1 for n in around["nodes"]) == 149
    assert around["left_out"] >= NOTES - 149  # the rest of its neighbours, and what lies beyond the ones shown
    assert around["nodes"][0]["id"] == HUB_NODE and around["nodes"][0]["hop"] == 0
    ids = {n["id"] for n in around["nodes"]}
    assert all(e["src"] in ids and e["dst"] in ids for e in around["edges"])

    small = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=HUB_NODE, limit=20, hops=1)
    assert len(small["nodes"]) == 20 and small["truncated"] and small["left_out"] == NOTES - 19

    runbook = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=RUNBOOK)
    assert not runbook["truncated"] and max(n["hop"] for n in runbook["nodes"]) == 2
    source = next(n for n in runbook["nodes"] if n["id"] == "source:docs")
    assert source["hop"] == 1 and source["hub"]  # 163 visible edges: not expanded past the first step
    assert not any(n["id"].startswith("docs:doc:note-") for n in runbook["nodes"])

    for params in ({"hops": 3}, {"limit": 151}, {"hops": 0}, {"limit": 0}):
        assert get(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=RUNBOOK, **params).status_code == 422


def test_a_grouped_neighbourhood_shows_the_most_connected_of_each_group_and_opens_one_whole(hub):
    whole = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=HUB_NODE, hops=1)
    (notes,) = whole["groups"]  # 160 sections mention KB-77
    assert (notes["key"], notes["total"], whole["group"]) == ("in:mentions:Section", NOTES, None)
    assert len(notes["shown"]) == 149 and whole["truncated"]  # ungrouped, the node limit cuts

    grouped = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=HUB_NODE, group=5)
    (notes,) = grouped["groups"]
    assert notes["total"] == NOTES and len(notes["shown"]) == 5 and grouped["group"] == 5
    assert grouped["neighbours"] == NOTES and not grouped["truncated"] and grouped["left_out"] == 0
    by_id = {n["id"]: n for n in grouped["nodes"]}
    assert {n["id"] for n in grouped["nodes"] if n["hop"] == 1} == set(notes["shown"])
    assert by_id[HUB_NODE]["degree"] == NOTES
    assert max(n["hop"] for n in grouped["nodes"]) == 2  # the walk goes on from the five shown
    assert all(e["src"] in by_id and e["dst"] in by_id for e in grouped["edges"])

    opened = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=HUB_NODE, group=5, hops=1, open=notes["key"])
    assert len(opened["groups"][0]["shown"]) == 149 and opened["truncated"]

    runbook = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=RUNBOOK, group=1, hops=1)
    groups = {g["key"]: g for g in runbook["groups"]}
    assert set(groups) == {"out:in_source:Source", "in:part_of:Section"}
    degrees = {n["id"]: n["degree"] for n in runbook["nodes"]}
    assert degrees["source:docs"] > HUB_DEGREE_SEEN  # the source of every document
    for group in groups.values():
        shown = [degrees[i] for i in group["shown"]]
        assert shown == sorted(shown, reverse=True)

    for params in ({"group": 0}, {"group": 151}):
        assert get(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=RUNBOOK, **params).status_code == 422


def test_a_grouped_neighbourhood_counts_only_what_the_reader_sees(hub):
    writer = ok(hub, "wendy", f"/v1/kg/{PROJECT}/neighbourhood", id=SHARED_NODE, group=5)
    reader = ok(hub, "ivan", f"/v1/kg/{PROJECT}/neighbourhood", id=SHARED_NODE, group=5)
    assert sum(g["total"] for g in reader["groups"]) == sum(g["total"] for g in writer["groups"]) - 1
    assert not any(i.startswith("deals:") for g in reader["groups"] for i in g["shown"])
    seen = {n["id"]: n["degree"] for n in reader["nodes"]}
    full = {n["id"]: n["degree"] for n in writer["nodes"]}
    assert seen[SHARED_NODE] == full[SHARED_NODE] - 1  # the contract's mention is not counted for the reader


def test_the_graph_shape_counts_the_visible_edges_by_kinds_and_type(hub):
    shape = ok(hub, "wendy", f"/v1/kg/{PROJECT}/shape")
    assert shape["graph"]["build_id"] == hub.build["id"]
    rows = {(r["src_kind"], r["rel"], r["dst_kind"]): r["count"] for r in shape["shape"]}
    assert rows[("Section", "mentions", "Requirement")] >= NOTES
    assert rows[("Document", "in_source", "Source")] == 2 + NOTES + 1
    counts = [r["count"] for r in shape["shape"]]
    assert counts == sorted(counts, reverse=True) and sum(counts) <= hub.build["edges"]

    internal = {
        (r["src_kind"], r["rel"], r["dst_kind"]): r["count"]
        for r in ok(hub, "ivan", f"/v1/kg/{PROJECT}/shape")["shape"]
    }
    assert internal[("Document", "in_source", "Source")] == 2 + NOTES  # the contract is customer
    assert sum(internal.values()) < sum(counts)
    assert ok(hub, "pat", f"/v1/kg/{PROJECT}/shape")["shape"] == []


def test_a_node_comes_with_its_label_source_uri_evidence_and_neighbours_by_edge_type(hub):
    detail = ok(hub, "ivan", f"/v1/kg/{PROJECT}/node", id=RUNBOOK)
    node = detail["node"]
    assert (node["id"], node["kind"], node["name"], node["status"]) == (RUNBOOK, "Document", "runbook", "parsed")
    assert node["label"]["level"] == "internal"
    assert node["props"]["source"] == "docs" and node["props"]["uri"] == "https://example.test/runbook"
    assert detail["graph"]["content_hash"] == hub.build["content_hash"]
    (evidence,) = detail["evidence"]
    assert (evidence["item"], evidence["source"], evidence["uri"]) == (RUNBOOK, "docs", "https://example.test/runbook")
    assert [(r["rel"], r["node"]) for r in detail["outgoing"]] == [("in_source", "source:docs")]
    assert {r["rel"] for r in detail["incoming"]} == {"part_of"}
    assert [r["node"] for r in detail["incoming"]] == [RUNBOOK_SECTION]  # its top section; the others are in it

    section = ok(hub, "ivan", f"/v1/kg/{PROJECT}/node", id=RUNBOOK_SECTION)
    assert {(r["rel"], r["node"]) for r in section["outgoing"]} >= {
        ("mentions", SHARED_NODE),
        ("part_of", RUNBOOK),
    }


def test_the_build_list_the_status_page_reads(hub):
    builds = ok(hub, "ivan", f"/v1/kg/{PROJECT}/builds")
    (build,) = builds["builds"]
    assert build["status"] == "succeeded" and build["content_hash"].startswith("sha256:")
    assert build["queued_at"] <= build["started_at"] <= build["finished_at"]
    assert build["error"] is None and build["nodes"] > 300 and build["runs"] == 2
    assert builds["jobs"] == []


# Refusals


def test_refusals(hub):
    stranger = live.bearer(live.insert_token(hub.db, "olga"))
    for path, params in (
        ("graph", {}),
        ("nodes", {"q": "runbook"}),
        ("node", {"id": RUNBOOK}),
        ("neighbourhood", {"id": RUNBOOK}),
        ("shape", {}),
    ):
        url = f"/v1/kg/{PROJECT}/{path}"
        assert hub.client.get(url, params=params).status_code == 401
        refused = get(hub, hub.admin, url, **params)  # a hub admin without a grant on the project
        assert refused.status_code == 403 and refused.json()["error"] == "forbidden", path
        assert get(hub, stranger, url, **params).status_code == 404  # no grant: the project does not show
        assert get(hub, "wendy", f"/v1/kg/no-such-project/{path}", **params).status_code == 404
    assert get(hub, "wendy", f"/v1/kg/{PROJECT}/nodes").status_code == 422  # q is required
    assert get(hub, "wendy", f"/v1/kg/{PROJECT}/nodes", q="x" * 201).status_code == 422
    assert get(hub, "wendy", f"/v1/kg/{PROJECT}/node").status_code == 422


def test_a_project_without_a_build(hub):
    assert ok(hub, "wendy", f"/v1/kg/{EMPTY}/graph") == {"graph": None, "kinds": [], "sources": []}
    assert ok(hub, "wendy", f"/v1/kg/{EMPTY}/shape") == {"graph": None, "shape": []}
    found = ok(hub, "wendy", f"/v1/kg/{EMPTY}/nodes", q="runbook")
    assert found["graph"] is None and found["results"] == []
    for path in ("node", "neighbourhood"):
        response = get(hub, "wendy", f"/v1/kg/{EMPTY}/{path}", id=RUNBOOK)
        assert response.status_code == 404 and "has no graph on the hub yet" in response.json()["message"]
