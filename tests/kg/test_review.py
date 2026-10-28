"""The review queue of a build (``evo_agents.kg.review``): the six rules, stable fingerprints, the store, what a
reader sees."""

from evo_agents.kg import review
from evo_agents.kg.build import build_project
from evo_agents.kg.search import PASSAGE_MARK
from evo_agents.kg.store import Clusters, Graph, Store
from tests.kg.projects import commit, make_project, write
from tests.kg.test_clusters import CUSTOMER, link, needs_engine, node, sync_build


def visible_to_internal(label: tuple) -> bool:
    return label[0] <= 1


def queue() -> Graph:
    """A small graph with clusters by hand: c1 holds code and a guide, c2 code alone."""
    graph = Graph()
    node(graph, "app:a.py", "File", "a.py", path="a.py", repo="app")
    node(graph, "app:b.py", "File", "b.py", path="b.py", repo="app")
    node(graph, "app:b.py::run", "Symbol", "run", file="app:b.py")
    node(graph, "lib:b.py", "File", "b.py", path="b.py", repo="lib")
    node(graph, "doc:guide.md", "Document", "Guide", path="docs/guide.md")
    node(graph, "doc:guide.md#use", "Section", "Use", item="doc:guide.md")
    node(graph, "doc:lonely.md", "Document", "Lonely", path="docs/lonely.md")
    node(graph, "doc:secret.md", "Document", "Secret", CUSTOMER, path="docs/secret.md")
    node(graph, "problem:1", "Problem", "Slow search")
    node(graph, "problem:2", "Problem", "slow  SEARCH!")
    node(graph, "problem:3", "Problem", "Slow search", CUSTOMER)
    for i in range(3):
        node(graph, f"app:img/{i}.png", "File", f"{i}.png", path=f"img/{i}.png")
        node(graph, f"plan:p{i}", "Plan", f"p{i}")
        node(graph, f"plan:p{i}#s", "PlanStep", "Write the tests")
        link(graph, f"plan:p{i}", "has_step", f"plan:p{i}#s")
    node(graph, "app:dist/x.js", "File", "x.js", path="dist/x.js")
    link(graph, "doc:guide.md#use", "mentions", "doc:guide.md")  # its own section does not count
    link(graph, "doc:lonely.md", "mentions", "doc:guide.md")
    link(graph, "app:a.py", "imports", "app:b.py")
    link(graph, "doc:guide.md", "mentions", "app:a.py")
    link(graph, "doc:guide.md#use", "mentions", "app:b.py")
    resolved = next(e for e, v in graph.edges.items() if v["src"] == "doc:guide.md#use" and v["dst"] == "app:b.py")
    graph.edges[resolved]["status"] = "resolved"
    graph.clusters = Clusters(
        clusters={
            "c1": {"level": 1, "parent": None, "label": "guide", "size": 3, "kinds": {}},
            "c2": {"level": 1, "parent": None, "label": "lib", "size": 1, "kinds": {}},
        },
        members={
            "app:a.py": ("c1", None),
            "app:b.py": ("c1", None),
            "doc:guide.md": ("c1", None),
            "doc:lonely.md": ("c1", None),
            "doc:secret.md": ("c1", None),
            "lib:b.py": ("c2", None),
        },
    )
    graph.mentions = {
        "doc:guide.md": {"dangling": 2, "ambiguous": 1, "samples": {"dangling": ["gone.py"], "ambiguous": ["util"]}},
    }
    return graph


def by_rule(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {rule: [] for rule in review.RULES}
    for item in items:
        out[item["rule"]].append(item)
    return out


def test_review_rules_find_each_kind_of_item():
    items = review.compute(queue())
    found = by_rule(items)
    dangling = found["dangling_mention"]
    assert [i["evidence"] for i in dangling] == [["doc:guide.md"]]
    assert dangling[0]["detail"]["mentions"] == 2 and dangling[0]["detail"]["samples"] == ["gone.py"]
    assert dangling[0]["cluster"] == "c1" and dangling[0]["action"] == "fix_mention"
    assert {i["evidence"][0] for i in found["unmentioned_document"]} == {"doc:lonely.md", "doc:secret.md"}
    assert [i["detail"]["label"] for i in found["cluster_without_knowledge"]] == ["lib"]
    assert [i["evidence"] for i in found["cluster_without_knowledge"]] == [["lib:b.py"]]
    dupes = found["possible_duplicate"]
    assert len(dupes) == 1 and set(dupes[0]["evidence"]) == {"problem:1", "problem:2", "problem:3"}
    ambiguous = {i["detail"].get("edge") or i["evidence"][0]: i for i in found["ambiguous_match"]}
    assert ambiguous["doc:guide.md"]["detail"]["mentions"] == 1
    edge = next(i for i in found["ambiguous_match"] if i["detail"].get("edge"))
    assert edge["evidence"] == ["doc:guide.md#use", "app:b.py", "lib:b.py"] and edge["detail"]["path"] == "b.py"
    noise = {(i["detail"]["kind"], i["detail"]["where"]) for i in found["bulk_noise"]}
    assert noise == {("image", "img"), ("step", None)}  # one generated file is not bulk
    assert review.counts(items) == {rule: len(found[rule]) for rule in review.RULES}


def test_review_fingerprints_hold_across_builds_and_hide_nothing_of_the_cluster_ids():
    first = {i["fingerprint"]: i for i in review.compute(queue())}
    graph = queue()
    graph.clusters.clusters["c9"] = graph.clusters.clusters.pop("c2")
    graph.clusters.members["lib:b.py"] = ("c9", None)
    node(graph, "doc:other.md", "Document", "Other", path="docs/other.md")
    graph.clusters.members["doc:other.md"] = ("c1", None)
    again = {i["fingerprint"]: i for i in review.compute(graph)}
    # every item but the cluster's (keyed by its id) keeps its fingerprint; the new document adds one
    assert set(first) - set(again) == {review.fingerprint("cluster_without_knowledge", "c2")}
    assert set(again) - set(first) == {
        review.fingerprint("cluster_without_knowledge", "c9"),
        review.fingerprint("unmentioned_document", "doc:other.md"),
    }
    assert all(fp.startswith("r:") and len(fp) == 18 for fp in again)


def test_review_needs_clusters():
    graph = queue()
    graph.clusters = None
    assert review.compute(graph) == []
    graph.clusters = Clusters()
    assert review.compute(graph) == []


def test_review_reader_sees_items_through_their_nodes():
    graph = queue()
    items = review.compute(graph)
    labels = {nid: (n["label"].level, 0, "U", None) for nid, n in graph.nodes.items()}
    seen = [review.visible_item(i, labels, visible_to_internal) for i in items]
    shown = [i for i in seen if i is not None]
    assert not any("doc:secret.md" in i["evidence"] for i in shown)
    dupes = [i for i in shown if i["rule"] == "possible_duplicate"]
    assert len(dupes) == 1 and set(dupes[0]["evidence"]) == {"problem:1", "problem:2"}
    assert dupes[0]["detail"]["count"] == 2 and "required" not in dupes[0]["detail"]
    assert all("clusters" not in i for i in shown)
    # a duplicate pair with one hidden node is no item for the reader
    stored = next(i for i in items if i["rule"] == "possible_duplicate")
    alone = {**labels, "problem:2": (2, 0, "U", None)}
    assert review.visible_item(stored, alone, visible_to_internal) is None
    # an item whose required node is hidden is absent; absent labels (another audience) count as hidden
    edge = next(i for i in items if i["detail"].get("edge"))
    assert review.visible_item(edge, {k: v for k, v in labels.items() if k != "app:b.py"}, visible_to_internal) is None


def test_review_names_normalise_accents_case_and_punctuation():
    assert review.normal("Tìm kiếm  CHẬM!") == review.normal("tim kiem cham")
    assert review.GENERATED.search("web/pnpm-lock.yaml") and review.GENERATED.search("app/x_pb2.py")
    assert not review.GENERATED.search("src/builder.py")


@needs_engine
def test_review_build_stores_items_counts_them_and_keeps_fingerprints(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = sync_build(project, verify=True)
    assert report.verify["match"], report.verify
    counts = report.coverage["review"]
    assert set(counts) == set(review.RULES)
    store = Store.for_project(project)
    items = store.review_items(report.build_id)
    assert sum(counts.values()) == len(items)
    assert all(set(i["evidence"]) <= store.alive_ids("nodes", report.build_id) for i in items)
    first = {i["fingerprint"] for i in items}
    store.close()

    again = build_project(project, force=True, verify=True)
    assert again.ok and again.verify["match"] and again.content_hash == report.content_hash
    store = Store.for_project(project)
    assert {i["fingerprint"] for i in store.review_items(again.build_id)} == first
    store.close()


@needs_engine
def test_the_passages_of_a_long_section_are_searched_but_never_review_items(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    write(tmp_path / "demo-app" / "docs/long.md", "# Long\n\n" + "Filler sentence. " * 400 + "\n\nThe end.\n")
    commit(tmp_path / "demo-app", "a long section")
    report = sync_build(project)
    store = Store.for_project(project)
    keys = [k for (k,) in store.db.execute("SELECT node_id FROM node_fts_rows")]
    assert any(PASSAGE_MARK in k for k in keys)  # the section is indexed as passages too
    items = store.review_items(report.build_id)
    assert items and all(PASSAGE_MARK not in i["fingerprint"] for i in items)
    assert all(PASSAGE_MARK not in e for i in items for e in i["evidence"])
    assert all(set(i["evidence"]) <= store.alive_ids("nodes", report.build_id) for i in items)
    store.close()
