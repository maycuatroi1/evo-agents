"""The audience of a label: principals a reader must all be (``chat:<id>``, ``grant:<project>:<level>``).

The lattice rules, the kg/1 key and how it is read (fail closed), and the four cases of the graph tier: a reader not
in the chat, a reader in the chat without the grant an older message names, a reader who leaves between two calls of
the same store, and elements derived from several items.
"""

import json
from pathlib import Path

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.policy import (
    TOP_AUDIENCE,
    Label,
    Policy,
    any_of,
    audience_below,
    audience_of,
    meet_all,
)
from evo_agents.kg.project import load_project_at
from evo_agents.kg.protocol import SCHEMAS, finalize_item, hello, item_hash, validate_message
from evo_agents.kg.serve import READ_META, Session
from evo_agents.kg.sessions import describe, read_session, record_read
from evo_agents.kg.store import SCHEMA_VERSION, StaleSchema, Store
from evo_agents.kg.sync import sync_project
from evo_agents.schema import Validator

C5, C6, GA, GB = "chat:5", "chat:6", "grant:a:internal", "grant:b:internal"
PLAN, M1, M2, M3, CODE = "chat:text:plan", "chat:text:m1", "chat:text:m2", "chat:text:m3", "code:K-7"
SHARED = "code:K-9"

OUTSIDER = frozenset({GA})  # a grant on the project, in no chat
LATE = frozenset({C5, GA})  # in chat 5, joined after the agent of project b left: no grant on b
EARLY = frozenset({C5, GA, GB})  # in chat 5 with a grant on b too
BOTH = frozenset({C5, C6, GA, GB})  # in chats 5 and 6


def label(audience=(), level=1) -> Label:
    return Label(level, 0, "U", frozenset({"demo"}), frozenset(audience))


# -- the lattice ---------------------------------------------------------------------------------------------------

SAMPLES = [frozenset(), frozenset({C5}), frozenset({C6}), frozenset({C5, C6}), frozenset({C5, GB}), TOP_AUDIENCE]


def test_join_is_the_union_meet_the_intersection_and_the_order_the_subset():
    assert label([C5]).join(label([C6])).audience == {C5, C6}
    assert label([C5, GB]).meet(label([C5, C6])).audience == {C5}
    assert label([C5]).below(label([C5, GA]))
    assert not label([C5, GB]).below(label([C5, GA]))
    assert label([]).below(label([])) and all(label([]).below(label(a)) for a in SAMPLES)  # no audience: any reader


def test_the_audience_order_is_a_lattice_whose_top_no_reader_reaches():
    for a in SAMPLES:
        for b in SAMPLES:
            la, lb = label(a), label(b)
            assert la.join(lb) == lb.join(la) and la.meet(lb) == lb.meet(la)
            assert la.below(la.join(lb)) and la.meet(lb).below(la)
            assert la.join(la.meet(lb)) == la and la.meet(la.join(lb)) == la  # absorption
            assert la.below(lb) == (la.join(lb) == lb) == (la.meet(lb) == la)
    assert label(TOP_AUDIENCE).meet(label([C5])).audience == {C5}  # a sink's clearance keeps the reader's audience
    assert not audience_below(TOP_AUDIENCE, frozenset({C5, C6, GA, GB}))
    assert Label(1, 0, "U", frozenset(), frozenset({"*", C5})).audience == TOP_AUDIENCE


def test_an_element_any_derivation_reveals_shows_to_whoever_sees_one_derivation_whole():
    assert any_of([label([]), label([C5])]).audience == frozenset()  # one derivation needs no audience
    assert any_of([label([C5]), label([C5, C6])]).audience == {C5}
    # Two chats: the intersection would show it to a reader in neither; it shows to a reader in both.
    assert any_of([label([C5]), label([C6])]).audience == {C5, C6}
    # The lower level comes from the chat 5 derivation, so its audience comes with it.
    low = any_of([label([], level=2), label([C5], level=0)])
    assert (low.level, low.audience) == (0, {C5})
    plain = [label([], level=2), Label(1, 0, "T", frozenset({"demo"}))]
    assert any_of(plain) == meet_all(plain)  # without audiences, the meet as before


@pytest.mark.parametrize(
    "written",
    ["chat:5", [C5, C5], [GB, C5], ["user:7"], [5], ["chat:05"], ["grant:b"], {"chat": 5}, [C5, "*"]],
)
def test_an_audience_that_cannot_be_read_is_the_highest(written):
    assert audience_of(written) == TOP_AUDIENCE
    raised = Policy("demo", {}).source_label("chat", {"level": "internal", "audience": written})
    assert raised.audience == TOP_AUDIENCE and raised.level == 3  # no declared source: the highest level too


def test_the_kg1_label_takes_an_audience_and_no_hash_covers_it():
    policy = Policy("demo", {"sources": [{"id": "chat", "label": {"level": "internal"}}]})
    assert policy.source_label("chat", {"audience": [C5, GB]}).audience == {C5, GB}
    assert policy.source_label("chat", {"audience": []}).audience == frozenset()
    assert policy.source_label("chat").audience == frozenset()
    item = {"id": "chat:text:x", "kind": "text", "rev": "1", "rev_time": "2026-10-19T00:00:00Z", "title": "x"}
    plain = finalize_item(item)
    shown = finalize_item({**item, "label": {"level": "internal", "audience": [C5, GB]}})
    assert validate_message(shown) == [] and shown["hash"] == plain["hash"] == item_hash(item)
    odd = finalize_item({**item, "label": {"audience": [GB, C5]}})
    assert validate_message(odd) == []  # read as the highest audience, not refused
    warned = Validator(SCHEMAS["item"]).validate({**plain, "label": {"audience": "chat:5"}})
    assert warned and {issue.severity for issue in warned} == {"warning"}


# -- the graph -----------------------------------------------------------------------------------------------------


def connector(ctx):
    """An in-process kg/1 source: the items of the JSON file its config names, each with its own audience."""
    spec = json.loads(Path(ctx.source["config"]["file"]).read_text())
    yield hello("audience", "1", list_complete=True, tombstones=True)
    for raw in spec["items"]:
        item = {
            "id": f"{ctx.source['id']}:text:{raw['key']}",
            "kind": "text",
            "rev": "1",
            "rev_time": "2026-10-19T00:00:00Z",
            "title": raw["title"],
            "uri": f"https://chat.example.test/{raw['key']}",
            "body": {"format": "text", "text": raw["text"]},
        }
        if "audience" in raw:
            item["label"] = {"audience": raw["audience"]}
        if raw.get("graph"):
            item["props"] = {"graph": raw["graph"]}
        yield finalize_item(item)
    yield {"type": "listing", "scope": f"{ctx.source['id']}:", "complete": True, "count": len(spec["items"])}
    yield {"type": "closed", "status": "ok"}


def mentions(*targets: str) -> list[dict]:
    return [{"src": "{self}", "rel": "mentions", "dst": t} for t in targets]


ITEMS = [
    {"key": "plan", "title": "Release plan", "text": "Release plan for the search service."},
    {
        "key": "m1",
        "title": "#1 alice 2026-10-19 09:00",
        "text": "The kiwi rollout breaks the search service.",
        "audience": [C5, GB],  # sent while the agent of project b was in chat 5
        "graph": {"nodes": [{"id": SHARED, "kind": "Code", "name": "K-9"}], "edges": mentions(PLAN)},
    },
    {
        "key": "m2",
        "title": "#2 bob 2026-10-19 10:00",
        "text": "Kiwi rollout fixed after the search service restart.",
        "audience": [C5],
        "graph": {"nodes": [{"id": CODE, "kind": "Code", "name": "K-7"}], "edges": mentions(PLAN)},
    },
    {
        "key": "m3",
        "title": "#3 carol 2026-10-19 11:00",
        "text": "Mango picks up what chat 5 said.",
        "audience": [C6],
        "graph": {"nodes": [{"id": CODE, "kind": "Code", "name": "K-7"}], "edges": mentions(M2)},
    },
]


def make(tmp_path: Path, home: Path, items: list[dict]):
    """A project whose one source gives ``items``, synced and built."""
    root = tmp_path / "demo-harness"
    root.mkdir(parents=True)
    spec = tmp_path / "chat.json"
    for raw in items:
        for edge in (raw.get("graph") or {}).get("edges", []):
            edge["src"] = f"chat:text:{raw['key']}"
    spec.write_text(json.dumps({"items": items}))
    (root / "harness.yaml").write_text("name: demo\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (root / "knowledge.yaml").write_text(
        f"""
version: 1
project: demo
policy:
  levels: [public, internal, customer]
  sinks:
    - id: agent
      kind: agent-session
      clearance: {{level: internal}}
sources:
  - id: chat
    connector: "python:tests.kg.test_audience:connector"
    config: {{file: '{spec}'}}
    label: {{level: internal, integrity: U}}
"""
    )
    project = load_project_at(root, home)
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


@pytest.fixture
def chats(tmp_path, kg_env):
    return make(tmp_path, kg_env, json.loads(json.dumps(ITEMS)))


def reading(project, audience) -> Session:
    session = Session(project, "agent")
    session.audience = frozenset(audience)
    return session


def data(session: Session, tool: str, **args) -> dict:
    found = session.call(tool, args)
    assert not found.get("isError"), found
    return found["structuredContent"]


def ids(found: dict, key: str = "results") -> set[str]:
    return {n["id"] for n in found[key]}


def seen(session: Session, node_id: str) -> bool:
    return not session.call("kg_node", {"id": node_id}).get("isError")


def test_the_store_keeps_the_audience_of_nodes_and_edges_and_reads_as_its_reader(chats):
    store = Store.open_readonly(chats)
    b = store.latest_ready()
    assert store.node(M2, b) is None and store.edges_of(PLAN, b, "in") == []  # no reader set: no audience seen
    assert {e["src"] for e in store.edges_of("source:chat", b, "in")} == {PLAN}
    store.reader = frozenset({C5})
    assert store.node(M2, b)["label"][3] == {C5} and store.node(M1, b) is None
    assert [(e["src"], e["label"][3]) for e in store.edges_of(PLAN, b, "in") if e["rel"] == "mentions"] == [(M2, {C5})]
    assert store.top_audience(b) == {C5, C6, GB}
    raw = store.db.execute("SELECT label_aud FROM nodes WHERE node_id = ? AND tx_to IS NULL", (M1,)).fetchone()[0]
    assert json.loads(raw) == [C5, GB]
    store.close()


def test_a_store_of_the_schema_before_audiences_is_stale(tmp_path):
    assert SCHEMA_VERSION == "4"
    old = Store(tmp_path / "graph.sqlite")
    with old.db:
        old.db.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    old.close()
    with pytest.raises(StaleSchema):
        Store(tmp_path / "graph.sqlite")


def test_a_reader_not_in_the_chat_sees_no_node_edge_or_count_of_it(chats):
    for audience in (frozenset(), OUTSIDER):  # kg serve's own reader holds no audience
        session = reading(chats, audience)
        assert ids(data(session, "kg_search", query="kiwi rollout")) == set()
        assert ids(data(session, "kg_search", query="search service")) == {PLAN}
        assert not any(seen(session, n) for n in (M1, M2, M3, CODE, SHARED))
        node = data(session, "kg_node", id=PLAN)
        assert node["in"] == [] and "in (" not in session.call("kg_node", {"id": PLAN})["content"][0]["text"]
        assert {e["node"] for e in data(session, "kg_node", id="source:chat")["in"]} == {PLAN}
        impact = data(session, "kg_impact", id=PLAN)
        assert impact["documents"] == [] and impact["impacted"] == []
        context = data(session, "kg_context", ids=[PLAN, "source:chat"])
        assert ids(context, "nodes") == {PLAN, "source:chat"} and len(context["edges"]) == 1
        store, b = session._store()
        assert not session.sees_all  # the build's labels are within the clearance, its audiences are not
        assert {kind for kind, *_ in store.kind_counts(b)} == {"Document", "Source"}


def test_a_search_matching_hidden_items_answers_as_the_graph_without_them(tmp_path, kg_env):
    with_chats = make(tmp_path / "with", kg_env / "with", json.loads(json.dumps(ITEMS)))
    without = make(tmp_path / "without", kg_env / "without", json.loads(json.dumps(ITEMS[:1])))
    calls = [
        ("kg_search", {"query": "search service"}),
        ("kg_search", {"query": "kiwi"}),
        ("kg_node", {"id": PLAN}),
        ("kg_node", {"id": "source:chat"}),
        ("kg_context", {"query": "search service"}),
        ("kg_impact", {"id": PLAN}),
        ("kg_path", {"from": PLAN, "to": "source:chat"}),
    ]
    for tool, args in calls:
        assert reading(with_chats, OUTSIDER).call(tool, args) == reading(without, OUTSIDER).call(tool, args), tool


def test_a_message_ranks_with_the_idf_the_rows_without_an_audience_give_its_words(tmp_path, kg_env):
    """``node_fts_aud`` holds a few rows: there a word that most of them hold gets next to no IDF, and a short message
    naming it would rank below a long public page that holds it once (``Store._as_public``)."""
    pages = [{"key": f"p{i}", "title": f"Release note {i}", "text": f"Notes of release {i}."} for i in range(12)]
    handbook = {"key": "handbook", "title": "Handbook", "text": "The kiwi " + "handbook " * 200}
    said = [{"key": f"m{i}", "title": f"#{i} alice", "text": f"kiwi {i}", "audience": [C5]} for i in range(3)]
    project = make(tmp_path, kg_env, [*pages, handbook, *said])
    found = [n["id"] for n in data(reading(project, [C5]), "kg_search", query="kiwi")["results"]]
    assert found == ["chat:text:m0", "chat:text:m1", "chat:text:m2", "chat:text:handbook"]
    assert [n["id"] for n in data(reading(project, [C6]), "kg_search", query="kiwi")["results"]] == [
        "chat:text:handbook"
    ]


def test_a_reader_who_joined_after_project_b_left_sees_later_messages_only(chats):
    late, early = reading(chats, LATE), reading(chats, EARLY)
    assert ids(data(late, "kg_search", query="kiwi rollout")) == {M2, PLAN}  # and what it mentions
    assert ids(data(early, "kg_search", query="kiwi rollout")) == {M1, M2, PLAN}
    assert not seen(late, M1) and seen(late, M2) and seen(early, M1)
    assert {e["node"] for e in data(late, "kg_node", id=PLAN)["in"]} == {M2}
    assert {d["node"]["id"] for d in data(late, "kg_impact", id=PLAN)["documents"]} == {M2}
    assert {d["node"]["id"] for d in data(early, "kg_impact", id=PLAN)["documents"]} == {M1, M2}
    found = late.call("kg_search", {"query": "kiwi rollout"})
    assert found["structuredContent"]["results"][0]["label"]["audience"] == [C5]
    assert found["content"][0]["_meta"][READ_META]["label"]["audience"] == [C5]


def test_a_reader_who_leaves_loses_the_chat_at_the_next_call_without_a_build(chats):
    session = reading(chats, LATE)
    build = data(session, "kg_search", query="kiwi")["build"]
    assert ids(data(session, "kg_search", query="kiwi")) == {M2, PLAN}
    session.audience = OUTSIDER  # left chat 5: the hub computes the audience again at each call
    after = data(session, "kg_search", query="kiwi")
    assert after["build"] == build and ids(after) == set() and not seen(session, M2)


def test_what_is_derived_from_several_items_shows_only_to_who_sees_them_all(chats):
    five, six, both = reading(chats, LATE), reading(chats, {C6, GA}), reading(chats, BOTH)
    # m3 (chat 6) mentions m2 (chat 5): the edge needs both, so it carries the join of their audiences.
    assert [e["node"] for e in data(both, "kg_node", id=M3)["out"] if e["rel"] == "mentions"] == [M2]
    assert [e for e in data(six, "kg_node", id=M3)["out"] if e["rel"] == "mentions"] == []
    assert [e for e in data(five, "kg_node", id=M2)["in"] if e["rel"] == "mentions"] == []
    # K-7 is declared by a message of each chat: neither reader of one chat alone sees it, a reader of both does.
    assert not seen(five, CODE) and not seen(six, CODE) and seen(both, CODE)
    assert data(both, "kg_node", id=CODE)["node"]["label"]["audience"] == [C5, C6]
    assert data(both, "kg_path", **{"from": M3, "to": PLAN})["found"]
    assert not data(six, "kg_path", **{"from": M3, "to": PLAN})["found"]
    store, b = both._store()
    assert both.sees_all and ("Code", 1, 0, "U", 2) in store.kind_counts(b)


def test_the_session_label_joins_the_audience_of_what_it_read(chats, tmp_path):
    home = tmp_path / "sessions-home"
    session = reading(chats, EARLY)
    meta = session.call("kg_search", {"query": "kiwi rollout"})["content"][0]["_meta"][READ_META]
    assert record_read("s-1", meta, home)["audience"] == [C5, GB]
    plain = reading(chats, frozenset()).call("kg_search", {"query": "search service"})["content"][0]["_meta"]
    assert "audience" not in plain[READ_META]["label"]  # a result without one names none, as before
    assert record_read("s-1", plain[READ_META], home)["audience"] == [C5, GB]  # the record only rises
    assert "audience" not in record_read("s-2", plain[READ_META], home)
    assert read_session("s-1", home)["audience"] == [C5, GB]
    assert "over demo, audience chat:5 grant:b:internal:" in describe(read_session("s-1", home))
