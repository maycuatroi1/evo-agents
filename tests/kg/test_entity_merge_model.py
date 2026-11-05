"""The model merges the entities of a build and summarizes their descriptions (evo_agents.kg.entity_merge): candidate
pairs, decisions memoized by pair and model, a merge recorded with its reason and undone by a decision, relations
summed and their keywords joined, summaries of one label only, all within the step's ceiling and sink."""

import json
import subprocess
import sys

import pytest
import yaml

from evo_agents.hub import kg_entity_relay
from evo_agents.kg import claude_code, entities, strategy
from evo_agents.kg import entity_links as links
from evo_agents.kg import entity_merge as merge
from evo_agents.kg.build import build_project, verify_build
from evo_agents.kg.memo import Memo
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

KINDS = ["Actor", "Place", "Concept", "Other"]
# The fake model: an item's answer by a word only that item holds; a merge says "same" for the pairs of the answers'
# "merges"; a summary counts its descriptions. Each call leaves a file in the log: its task and its prompt.
MODEL = """import json, pathlib, sys, uuid
req = json.load(sys.stdin)
answers = json.load(open(sys.argv[1]))
task = req.get("task") or "item"
pathlib.Path(sys.argv[2], uuid.uuid4().hex).write_text(json.dumps({"task": task, "text": req["text"]}))
usage = {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}
if task == "merge":
    same = [sorted(p) for p in answers.get("merges", [])]
    out = []
    for line in req["text"].splitlines():
        if ". A: " in line:
            n, a = line.split(". A: ", 1)
            pair = [int(n), a.split(" (")[0]]
        elif line.strip().startswith("B: "):
            b = line.strip()[3:].split(" (")[0]
            out.append({"pair": pair[0], "same": sorted([pair[1], b]) in same, "reason": f"{pair[1]} is {b}"})
    print(json.dumps({"decisions": out, "usage": usage}))
elif task == "summary":
    n = sum(1 for line in req["text"].splitlines() if line.startswith("- "))
    print(json.dumps({"summary": f"summary of {n}", "usage": usage}))
else:
    for marker, answer in answers.items():
        if marker != "merges" and marker in req["text"]:
            print(json.dumps({**answer, "usage": usage}))
            break
    else:
        print(json.dumps({"nodes": [], "edges": [], "usage": usage}))
"""


def ent(name, type_, description="", evidence="x"):
    return {"name": name, "type": type_, "description": description, "evidence": evidence}


def rel(src, dst, keywords, strength, description=""):
    return {"src": src, "dst": dst, "keywords": keywords, "strength": strength, "description": description}


def setup(tmp_path, kg_env, answers, *, docs=None, extra=None, level="internal", app_level="internal"):
    project = make_project(tmp_path, kg_env, level=level, app_level=app_level)
    if docs:
        app = tmp_path / "demo-app"
        for name, text in docs.items():
            write(app / "docs" / name, text)
        commit(app, "docs")
    script, answers_file, log = tmp_path / "model.py", tmp_path / "answers.json", tmp_path / "calls"
    script.write_text(MODEL)
    answers_file.write_text(json.dumps(answers))
    log.mkdir()
    mapping = {
        "model": "fake-1",
        "command": [sys.executable, str(script), str(answers_file), str(log)],
        "style": "lightrag",
        "node_kinds": KINDS,
        "relations": ["relates_to"],
        "items": ["markdown"],
        **(extra or {}),
    }
    data = {
        "version": 1,
        "name": "graph",
        "knowledge": {"ontology": {"node_kinds": KINDS, "relations": ["relates_to"]}},
        "settings": {"entities": mapping},
        "steps": ["entities"],
    }
    chosen = project.harness.root / "strategies" / "graph.yaml"
    write(chosen, yaml.safe_dump(data))
    strategy.apply(strategy.load_file(chosen), project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    return project, log


def calls(log, task=None) -> list[dict]:
    found = [json.loads(p.read_text()) for p in log.iterdir()]
    return [c for c in found if task is None or c["task"] == task]


def graph(project):
    """(entity nodes: id -> (kind, name, props), edges touching an entity: (src, rel, dst) -> props)."""
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        live = "tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)"
        nodes = {
            r[0]: (r[1], r[2], json.loads(r[3]))
            for r in store.db.execute(
                f"SELECT node_id, kind, name, props FROM nodes WHERE node_id LIKE 'entity:%' AND {live}", (b, b)
            )
        }
        edges = {
            (r[0], r[1], r[2]): json.loads(r[3])
            for r in store.db.execute(
                f"SELECT src, rel, dst, props FROM edges WHERE (src LIKE 'entity:%' OR dst LIKE 'entity:%') AND {live}",
                (b, b),
            )
        }
        return nodes, edges
    finally:
        store.close()


OBAMA = {
    "Intro": {
        "nodes": [ent("Barack Obama", "Person", "The 44th president."), ent("Hawaii", "Location", "A US state.")],
        "edges": [rel("Barack Obama", "Hawaii", "born in", 7, "He was born there.")],
    },
    "Repos: app": {
        "nodes": [ent("Obama", "Person", "A president."), ent("Hawaii", "Location", "Islands.")],
        "edges": [rel("Hawaii", "Obama", ["home", "Born in"], 3, "His home state.")],
    },
    "merges": [["Barack Obama", "Obama"]],
}


# -- candidates ---------------------------------------------------------------------------------------------------


def stated(kind, name, item="i"):
    return links.Stated(kind, name, (), 0.8, item, (1, 0, 0))


def test_candidates_are_near_vectors_one_name_in_two_kinds_and_near_names_of_one_kind():
    groups = links.group(
        [
            stated("Actor", "Barack Obama"),
            stated("Actor", "Obama"),
            stated("Place", "Washington"),
            stated("Actor", "Washington"),
            stated("Concept", "Organization"),
            stated("Concept", "Organisation"),
            stated("Concept", "Car"),
            stated("Concept", "Automobile"),
            stated("Concept", "Garden"),
        ]
    )
    car, auto = groups.id_of("Concept", "Car"), groups.id_of("Concept", "Automobile")
    got = merge.candidates(groups, [[min(car, auto), max(car, auto), 0.91]])
    assert got == [
        (auto, car, "vectors"),
        ("entity:Actor:washington", "entity:Place:washington", "same name"),
        ("entity:Concept:organisation", "entity:Concept:organization", "near names"),
        ("entity:Actor:barack-obama", "entity:Actor:obama", "near names"),
    ], "near names: the more alike first"
    assert merge.candidates(groups, [], limit=1) == [got[1]], "the ceiling keeps the first reasons first"


def test_a_closed_mapping_merges_nothing_unless_it_asks():
    assert not merge.merge_on({"model": "m"}) and merge.merge_on({"model": "m", "style": "lightrag"})
    assert merge.merge_on({"model": "m", "merge": True}) and not merge.merge_on({"style": "lightrag", "merge": False})
    with pytest.raises(entities.EntitiesError, match="merge must be true or false"):
        entities.check({"model": "m", "command": ["x"], "node_kinds": ["Concept"], "merge": "yes"})


# -- merge ----------------------------------------------------------------------------------------------------------


def test_the_model_merges_a_pair_records_why_sums_the_relation_and_memoizes_its_decision(tmp_path, kg_env):
    project, log = setup(tmp_path, kg_env, OBAMA)
    report = build_project(project)
    assert report.ok, report.errors
    cov = report.coverage["entities"]
    nodes, edges = graph(project)
    actors = {nid: n for nid, n in nodes.items() if n[0] == "Actor"}
    assert list(actors) == ["entity:Actor:barack-obama"], "one node for both names"
    (made,) = [(k, p) for k, p in edges.items() if k[1] == merge.MERGE_REL]
    (src, _, doc), props = made
    assert src == "entity:Actor:barack-obama" and props["name"] == "Obama" and props["with"] == "Barack Obama"
    assert props["reason"] and props["model"] == "fake-1" and props["why"] == "near names"
    assert "CLUSTER.md" in doc, "the edge goes to the document whose name was folded in"
    relation = edges[("entity:Actor:barack-obama", "relates_to", "entity:Place:hawaii")]
    assert relation["weight"] == 10.0 and relation["sources"] == 2, "both directions, after the merge, are one"
    assert relation["keywords"] == "born in, home"
    assert relation["summary" if "summary" in relation else "descriptions"]
    assert cov["merge"]["same"] == 1 and cov["merge"]["asked"] >= 1 and not cov["merge"].get("pending")
    assert len(calls(log, "merge")) == 1 and cov["calls"] == len(calls(log))
    before = len(calls(log))
    for cold in (False, True):
        assert verify_build(project, report.content_hash, cold=cold)["match"]
    assert len(calls(log)) == before, "a verification reads the decisions from the memo"
    write(tmp_path / "demo-app/docs/old.md", "# Old\n\nNotes rewritten.\n")
    commit(tmp_path / "demo-app", "old")
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok
    assert len(calls(log, "merge")) == 1, "the pair's decision is memoized by the pair and the model"


def test_a_pair_the_model_calls_two_stays_two_and_a_closed_mapping_asks_nothing(tmp_path, kg_env):
    answers = {**OBAMA, "merges": []}
    project, log = setup(tmp_path, kg_env, answers)
    cov = build_project(project).coverage["entities"]
    nodes, edges = graph(project)
    assert {"entity:Actor:barack-obama", "entity:Actor:obama"} <= set(nodes)
    assert cov["merge"]["same"] == 0 and not [k for k in edges if k[1] == merge.MERGE_REL]
    project, log = setup(tmp_path / "closed", kg_env, OBAMA, extra={"merge": False})
    cov = build_project(project).coverage["entities"]
    assert "merge" not in cov and not calls(log, "merge") and not calls(log, "summary")


class FakeStep:
    def __init__(self, decided):
        self.decided, self.counts = decided, {}

    def merges(self, cands, seen):
        return [(a, b, why, d) for a, b, why in cands for x, y, d in self.decided if {x, y} == {a, b}]

    def tally(self, what, key, n=1):
        self.counts[(what, key)] = self.counts.get((what, key), 0) + n


class Rec:
    def __init__(self, record):
        self.record, self.item_id = record, record["id"]


def answer(item, names, rank=(1, 0, 0)):
    nodes = [{"kind": "Actor", "name": n, "conf": 0.8, "evidence": "", "description": f"of {n}"} for n in names]
    return (item, f"{item}#", True, {"nodes": nodes, "edges": []}, rank, None)


def test_a_decision_that_deprecates_the_merge_edge_undoes_the_merge_and_keeps_applying():
    answers = [answer("doc:a", ["Barack Obama"], (0, 0, 0)), answer("doc:b", ["Obama"])]
    stated_ = [links.Stated("Actor", n["name"], (), 0.8, a[0], a[4]) for a in answers for n in a[3]["nodes"]]
    groups = links.group(stated_)
    pair = ("entity:Actor:barack-obama", "entity:Actor:obama", {"same": True, "reason": "one man"})
    config = {"model": "fake-1", "style": "lightrag"}
    folded, made = entities._merge(config, answers, stated_, groups, [], FakeStep([pair]), None)
    assert folded.id_of("Actor", "Obama") == "entity:Actor:barack-obama"
    ((item, (keep, doc, props), units),) = made
    assert (keep, doc, item) == ("entity:Actor:barack-obama", "doc:b", "doc:b") and props["reason"] == "one man"
    assert units == ["doc:a#", "doc:b#"], "the edge is labelled as both items the model read"
    decisions = {
        "version": 1,
        "decisions": [
            {"decision": 7, "kind": "deprecate", "from": keep, "rel": "merged_from", "to": doc, "reason": "two people"}
        ],
    }
    curation = Rec({"id": "plugin.hub_decisions:binding:decisions", "kind": "binding", "props": {"curation": True}})
    source = ([curation], lambda rec: yaml.safe_dump(decisions))
    step = FakeStep([pair])
    folded, made = entities._merge(config, answers, stated_, groups, [], step, source)
    assert folded.id_of("Actor", "Obama") == "entity:Actor:obama", "the merge is undone"
    ((_, (keep, doc, props), _),) = made
    assert (keep, doc) == ("entity:Actor:barack-obama", "doc:b") and props["undone"] is True
    assert step.counts[("merge", "undone")] == 1


# -- summaries ------------------------------------------------------------------------------------------------------


def test_eight_descriptions_of_one_label_get_a_summary_that_no_other_label_mixes_into(tmp_path, kg_env):
    docs = {f"h{i}.md": f"# H{i}\n\nIsland note marker{i}.\n" for i in range(8)}
    answers = {f"marker{i}": {"nodes": [ent("Hawaii", "Place", f"Fact {i} of the islands.")]} for i in range(8)}
    answers["Repos: app"] = {"nodes": [ent("Hawaii", "Place", "Secret harness fact.")]}
    project, log = setup(tmp_path, kg_env, answers, docs=docs, level="customer", app_level="internal")
    report = build_project(project)
    assert report.ok, report.errors
    nodes, edges = graph(project)
    _, _, props = nodes["entity:Place:hawaii"]
    assert props["summary"] == "summary of 8" and props["sources"] == 8, "the internal items, the most public"
    (asked,) = calls(log, "summary")
    assert "Fact 0" in asked["text"] and "Secret harness fact" not in asked["text"], "one label only"
    mentions = {k[0]: p for k, p in edges.items() if k[1] == "mentions" and k[2] == "entity:Place:hawaii"}
    harness = [p for item, p in mentions.items() if "CLUSTER.md" in item]
    assert harness and "summary" not in harness[0] and harness[0]["description"] == "Secret harness fact."
    assert sum(1 for p in mentions.values() if p.get("summary") == "summary of 8") == 8
    assert report.coverage["entities"]["summaries"]["made"] == 1
    write(tmp_path / "demo-app/docs/old.md", "# Old\n\nNotes rewritten.\n")
    commit(tmp_path / "demo-app", "old")
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok and len(calls(log, "summary")) == 1, "memoized by the descriptions and model"


def test_fewer_descriptions_are_kept_as_they_are():
    assert not merge.needs_summary(["a"] * 7) and merge.needs_summary(["a"] * 8)
    assert merge.needs_summary(["x" * 4801]), "over SUMMARY_TOKENS tokens"
    assert merge.merged_keywords(["born in, home", "Born In", "state"]) == "born in, home, state"


# -- ceiling, sink, relay -------------------------------------------------------------------------------------------


def step_of(config, tmp_path, policy=None):
    cov = entities._coverage(config, False)
    step = entities._Step(
        config, Memo(tmp_path / "memo.sqlite"), cov, ["Actor"], ["relates_to"], offline=False, outbox=None, inbox={}
    )
    step.policy = policy
    return step, cov


SEEN = {
    "a": merge.Seen("Actor", "Barack Obama", "", "doc:a", "public"),
    "b": merge.Seen("Actor", "Obama", "", "doc:b", "secret"),
}


def test_a_merge_past_the_ceiling_waits_for_a_later_build(tmp_path):
    config = {"model": "fake-1", "command": [sys.executable, "-c", "raise SystemExit(9)"], "max_calls": 1}
    step, cov = step_of(config, tmp_path)
    step.spend.calls = 1
    assert step.merges([("a", "b", "near names")], SEEN) == []
    assert cov["merge"]["pending"] == 1 and "max_calls 1" in cov["stopped"]
    assert entities.unfinished({"entities": cov}), "the next build is not skipped"


def test_a_label_the_sink_does_not_clear_is_never_sent(tmp_path):
    class Policy:
        sinks = {"llm": type("Sink", (), {"kind": "llm-backend"})()}

        def allows(self, label, sink):
            return label == "public"

    config = {"model": "fake-1", "command": [sys.executable, "-c", "raise SystemExit(9)"], "sink": "llm"}
    step, cov = step_of(config, tmp_path, Policy())
    assert step.merges([("a", "b", "near names")], SEEN) == [] and cov["merge"] == {"refused": 1}
    assert step.summary("x", ["one"] * 8, "secret") is None and cov["summaries"] == {"refused": 1}
    assert step.spend.calls == 0


def test_the_relay_and_claude_code_carry_a_task(tmp_path):
    rows = kg_entity_relay.clean_answers({"k": {"decisions": [{"pair": 1, "same": True}, 3], "summary": "s"}})
    assert rows["k"]["decisions"] == [{"pair": 1, "same": True}] and rows["k"]["summary"] == "s"
    seen = {}

    def runner(argv, **kw):
        seen["input"], seen["system"] = kw["input"], argv[-1]
        result = {"result": '{"decisions": [{"pair": 1, "same": false}]}', "subtype": "success", "usage": {}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(result), stderr="")

    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    request = {"task": "merge", "text": "PROMPT AS IS", "model": "m", "node_kinds": [], "relations": []}
    got = claude_code.answer(request, env={claude_code.BINARY_ENV: str(claude)}, runner=runner)
    assert seen == {"input": "PROMPT AS IS", "system": merge.SYSTEM} and got["decisions"][0]["same"] is False
    with pytest.raises(claude_code.ClaudeError, match="unknown task"):
        claude_code.answer({**request, "task": "drop"}, env={claude_code.BINARY_ENV: str(claude)}, runner=runner)


def test_a_deferred_task_goes_to_the_outbox_and_its_answer_comes_back_by_key(tmp_path):
    config = {"model": "fake-1", "command": ["claude"], "sink": "llm"}
    step, cov = step_of(config, tmp_path)
    step.defer, step.outbox = True, []
    step.allowed = lambda label: True
    assert step.merges([("a", "b", "near names")], SEEN) == []
    (request,) = step.outbox
    assert request["task"] == "merge" and "Barack Obama" in request["text"] and request["item"] == ""
    step2, _ = step_of(config, tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    step2.allowed = lambda label: True
    step2.inbox = {request["key"]: {"decisions": [{"pair": 1, "same": True, "reason": "r"}]}}
    ((a, b, _, d),) = step2.merges([("a", "b", "near names")], SEEN)
    assert (a, b, d["same"]) == ("a", "b", True)


# -- the props a reader sees ----------------------------------------------------------------------------------------


def test_a_relation_shows_the_props_of_its_most_public_item(tmp_path, kg_env):
    answers = {
        "Intro": {
            "nodes": [ent("Search", "Concept"), ent("Server", "Concept")],
            "edges": [rel("Search", "Server", "runs on", 2, "Public words.")],
        },
        "Repos: app": {
            "nodes": [ent("Search", "Concept"), ent("Server", "Concept")],
            "edges": [rel("Search", "Server", "secret", 9, "Secret words.")],
        },
        "merges": [],
    }
    for level, app_level in (("customer", "internal"), ("internal", "customer")):
        project, _ = setup(tmp_path / level, kg_env, answers, level=level, app_level=app_level)
        assert build_project(project).ok
        _, edges = graph(project)
        props = edges[("entity:Concept:search", "relates_to", "entity:Concept:server")]
        public = "Public words." if app_level == "internal" else "Secret words."
        assert props["description"] == public and "sources" not in props, "one label each: nothing shared"
