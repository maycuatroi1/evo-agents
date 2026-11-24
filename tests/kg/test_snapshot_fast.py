"""The fast paths of a snapshot (plan kg-sound-fast-optimal, criterion a3): the full-text search by Block-Max WAND
over CSR postings answers what scoring every matching row answers; the walk as a sparse matrix-vector product
answers what the loop answers; each path has a fallback of the standard library that answers the same; a reader of
any one of an element's guards reads it; a delta applied to a snapshot gives the snapshot a full read of the rows
after it gives; and kg_search answers within its budget on a graph the size of the evo-agents one."""

from __future__ import annotations

import dataclasses
import gc
import itertools
import json
import random
import statistics
import time
from collections import Counter

import pytest

np = pytest.importorskip("numpy")

from evo_agents.kg import ppr, snapshot_carry, snapshot_csr, snapshot_fts, snapshot_masks  # noqa: E402
from evo_agents.kg.snapshot import Snapshot, StoreRows  # noqa: E402
from evo_agents.kg.snapshot_delta import Delta  # noqa: E402
from evo_agents.kg.snapshot_fts import Table, trigrams, wand_top_k, words  # noqa: E402
from evo_agents.kg.snapshot_search import SnapshotSearch  # noqa: E402

WORDS = (
    "northwind",
    "lumen",
    "ledger",
    "invoice",
    "shipment",
    "harbor",
    "beacon",
    "orbit",
    "quill",
    "ember",
    "tally",
    "route",
    "parcel",
    "cargo",
    "manifest",
    "tariff",
)
PUBLIC = (0, 0, "U", None)
OPS = '["chat:41"]'  # the audience of a private chat item


SYLLABLES = ("ka", "lo", "mi", "ve", "ru", "ta", "no", "si", "de", "pa", "zu", "ri", "go", "fe")
# a vocabulary as a code base's: the domain words and some thousands of others, each drawn by Zipf's law (the
# ``i``-th most frequent word ``1 / i`` as often as the first), so a few words are in most rows and most in few
VOCABULARY = WORDS + tuple(a + b + c for a in SYLLABLES for b in SYLLABLES for c in SYLLABLES)
ZIPF = tuple(itertools.accumulate(1 / (i + 1) for i in range(len(VOCABULARY))))


def text(rnd: random.Random, n: int = 12) -> str:
    return " ".join(rnd.choices(VOCABULARY, cum_weights=ZIPF, k=n))


def node(nid: str, kind: str, name: str, props: dict, aud: str | None = None, guards: str | None = None) -> tuple:
    return (nid, kind, name, json.dumps(props, sort_keys=True), "parsed", 1.0, 0, 0, "U", "[]", aud, guards)


def edge(src: str, rel: str, dst: str, n: int) -> tuple:
    return (f"e:{n:08d}", src, rel, dst, "{}", "parsed", 1.0, 0, 0, "U", None, None)


def graph(files: int, per_file: int, calls: int, seed: int = 7) -> StoreRows:
    """A graph of ``files`` files of ``per_file`` symbols each, with ``calls`` calls between symbols, every node a
    full-text row, a few in a private chat's audience, and the provenance of the first files."""
    rnd = random.Random(seed)
    nodes, edges, keyed, symbols, units, wheres = [], [], [], [], [], []
    for f in range(files):
        path = f"src/{WORDS[f % len(WORDS)]}/mod_{f}.py"
        fid = f"lumen-api:file:{path}"
        aud = OPS if f % 97 == 5 else None
        nodes.append(node(fid, "File", f"mod_{f}.py", {"path": path}, aud))
        if aud is None:
            keyed.append((fid, (fid, "File", f"mod_{f}.py", path, text(rnd))))
        else:
            keyed.append((f"{fid}#a{f}", (fid, "File", f"mod_{f}.py", path, text(rnd), aud)))
        for s in range(per_file):
            name = f"{rnd.choice(WORDS).title()}{rnd.choice(WORDS).title()}{s}"
            sid = f"symbol:lumen-api:{path}::{name}"
            symbols.append(sid)
            nodes.append(node(sid, "Symbol", name, {"path": path, "file": fid}))
            keyed.append((sid, (sid, "Symbol", name, path, text(rnd))))
            edges.append(edge(fid, "contains", sid, len(edges)))
        if f < 20:
            units.append((f"u:{f}", f"item:{f}", "", "r1", f"h{f}", "lumen-docs", None, None, 0, 0))
            wheres.append((fid, f"u:{f}", None))
    for _ in range(calls):
        edges.append(edge(rnd.choice(symbols), "calls", rnd.choice(symbols), len(edges)))
    return StoreRows(
        "sha256:one",
        tuple(sorted(nodes)),
        tuple(edges),
        tuple(sorted(keyed)),
        {},
        ((0, 0), frozenset({"chat:41"})),
        units=tuple(units),
        wheres=tuple(wheres),
        summary={"ready": True},
    )


def changed(base: StoreRows, seed: int = 11) -> tuple[StoreRows, Delta]:
    """The rows after a typical pull request on ``base``, and the delta naming what it changed: nodes renamed,
    dropped and added, edges reweighed, dropped and added, text rewritten and added, provenance changed."""
    rnd = random.Random(seed)
    nodes, edges, keyed = list(base.nodes), list(base.edges), dict(base.keyed)
    touched_nodes, touched_edges, touched_keys = set(), set(), set()
    for i in rnd.sample(range(len(nodes)), 12):
        row = list(nodes[i])
        row[2] = f"{row[2]}Renamed"
        nodes[i] = tuple(row)
        touched_nodes.add(row[0])
    for i in sorted(rnd.sample(range(len(nodes)), 3), reverse=True):
        touched_nodes.add(nodes[i][0])
        keyed.pop(nodes[i][0], None)
        touched_keys.add(nodes[i][0])
        del nodes[i]
    ids = [n[0] for n in nodes]
    for k in range(6):
        nid = f"symbol:lumen-api:src/quill/new_{k}.py::Northwind{k}"
        nodes.append(node(nid, "Symbol", f"Northwind{k}", {"path": f"src/quill/new_{k}.py"}))
        touched_nodes.add(nid)
        keyed[nid] = (nid, "Symbol", f"Northwind{k}", f"src/quill/new_{k}.py", "a northwind lumen rollout")
        touched_keys.add(nid)
        edges.append(edge(rnd.choice(ids), "calls", nid, 10_000_000 + k))
        touched_edges.add(edges[-1][0])
    for i in rnd.sample(range(len(edges) - 6), 40):
        row = list(edges[i])
        row[6] = 0.8
        edges[i] = tuple(row)
        touched_edges.add(row[0])
    for i in sorted(rnd.sample(range(len(edges) - 6), 8), reverse=True):
        touched_edges.add(edges[i][0])
        del edges[i]
    for key in rnd.sample(sorted(k for k in keyed if k not in touched_keys), 15):
        row = keyed[key]
        keyed[key] = (*row[:4], f"{row[4]} tariff manifest rollout", *row[5:])
        touched_keys.add(key)
    alive = {n[0] for n in nodes}
    units = list(base.units[1:]) + [("u:new", "item:new", "", "r2", "hn", "lumen-docs", None, OPS, 0, 0)]
    wheres = [w for w in base.wheres if w[1] != base.units[0][0]] + [(ids[0], "u:new", None)]
    elems = {base.units[0][0], "u:new", base.wheres[0][0], ids[0]}
    after = dataclasses.replace(
        base,
        content_hash="sha256:two",
        nodes=tuple(sorted(nodes)),
        edges=tuple(edges),
        keyed=tuple(sorted(keyed.items())),
        units=tuple(sorted(units)),
        wheres=tuple(sorted(wheres, key=lambda w: (w[0], w[1]))),
    )
    by_id, by_edge = {n[0]: n for n in nodes}, {e[0]: e for e in edges}
    provenance = {
        e: (
            tuple(u for u in units if u[0] == e),
            tuple(w for w in after.wheres if w[0] == e),
            (),
        )
        for e in elems
    }
    delta = Delta(
        base=base.content_hash,
        content_hash=after.content_hash,
        top=after.top,
        summary=after.summary,
        build=2,
        node_ids=tuple(sorted(touched_nodes)),
        nodes=tuple(by_id[i] for i in sorted(touched_nodes) if i in by_id),
        edge_ids=tuple(sorted(touched_edges)),
        edges=tuple(by_edge[i] for i in sorted(touched_edges) if i in by_edge)
        + tuple(e for e in edges if (e[1] in touched_nodes or e[3] in touched_nodes) and e[1] in alive),
        text_keys=tuple(sorted(touched_keys)),
        keyed=tuple((k, keyed[k]) for k in sorted(touched_keys) if k in keyed),
        provenance=provenance,
    )
    return after, delta


def counted(rows) -> Counter:
    return Counter({tuple(r[:-1]): r[-1] for r in rows})


def assert_same(got: Snapshot, want: Snapshot) -> None:
    """``got`` holds what ``want`` holds; counts by key, in any order."""
    for name in ("content_hash", "ids", "nodes", "edges", "src", "dst", "names", "aliases", "bodies", "units"):
        assert getattr(got, name) == getattr(want, name), name
    assert got.where == want.where and got.derivations == want.derivations
    for name in ("node_counts", "edge_counts", "shape", "unit_counts", "item_counts"):
        assert counted(getattr(got, name)) == counted(getattr(want, name)), name
    mine, theirs = snapshot_masks.of(got), snapshot_masks.of(want)
    assert [mine.keys[i] for i in mine.node] == [theirs.keys[i] for i in theirs.node]
    assert [mine.keys[i] for i in mine.edge] == [theirs.keys[i] for i in theirs.edge]
    assert got.partitions.keys() == want.partitions.keys()
    for key, part in want.partitions.items():
        other = got.partitions[key]
        assert (other.out_start, other.out_edges, other.in_start, other.in_edges) == (
            part.out_start,
            part.out_edges,
            part.in_start,
            part.in_edges,
        ), key
    assert got.text.rows == want.text.rows
    for name in ("public", "audience", "tri", "code"):
        assert_same_table(getattr(got.text, name), getattr(want.text, name))


def assert_same_table(got: Table, want: Table) -> None:
    assert got.rows == want.rows and got.vocabulary == want.vocabulary and list(got.sizes) == list(want.sizes)
    for query in ("northwind", "lumen ledger", "rollout OR tariff", "led*", '"cargo manifest"', "orbit AND quill"):
        assert got.search(query, (10.0, 5.0, 1.0)) == want.search(query, (10.0, 5.0, 1.0)), query


def best(table: Table, query: str, k: int, keep=None) -> list[tuple[int, float]]:
    """The rows scoring every match keeps among the ``k`` best, every row tied with the ``k``-th included."""
    found = table.search(query, (10.0, 5.0, 1.0), keep)
    if len(found) <= k:
        return found
    kth = sorted(score for _, score in found)[k - 1]
    return [(n, s) for n, s in found if s <= kth]


@pytest.fixture(scope="module")
def table() -> Table:
    rnd = random.Random(3)
    return Table([(text(rnd, 2), f"src/{rnd.choice(WORDS)}.py", text(rnd, 30)) for _ in range(3000)], words)


@pytest.mark.parametrize(
    "query",
    ["northwind", "lumen ledger", "northwind OR harbor OR cargo", "led*", '"cargo manifest"', "orbit AND quill"],
)
@pytest.mark.parametrize("k", [1, 10, 50])
def test_the_block_max_wand_top_k_is_the_top_k_of_scoring_every_match(table, query, k):
    got = wand_top_k(table, query, (10.0, 5.0, 1.0), k)
    assert [n for n, _ in got] == [n for n, _ in best(table, query, k)]
    assert [s for _, s in got] == pytest.approx([s for _, s in best(table, query, k)])
    keep = np.arange(len(table)) % 3 != 0  # a reader's mask, as ``TextIndex.kept`` gives it
    kept = wand_top_k(table, query, (10.0, 5.0, 1.0), k, keep)
    assert [n for n, _ in kept] == [n for n, _ in best(table, query, k, keep)]


FTS_QUERIES = ("northwind", "lumen ledger", "northwind OR harbor", "led*", '"cargo manifest"', "orbit AND quill")


def test_without_numpy_a_table_answers_what_it_answers_with_it(monkeypatch, table):
    weights = (10.0, 5.0, 1.0)
    fast = {q: (table.search(q, weights), wand_top_k(table, q, weights, 10)) for q in FTS_QUERIES}
    monkeypatch.setattr(snapshot_fts, "_np", None)
    plain = Table(table.rows, words)
    assert not plain.fast
    for query, (every, top) in fast.items():
        assert [n for n, _ in plain.search(query, weights)] == [n for n, _ in every]
        assert [s for _, s in plain.search(query, weights)] == pytest.approx([s for _, s in every])
        assert [n for n, _ in wand_top_k(plain, query, weights, 10)] == [n for n, _ in top]


def test_a_trigram_phrase_matches_the_rows_holding_it_as_a_substring(monkeypatch):
    rows = [("northwindledger", "src/northwind/ledger.py"), ("lumenharbor", "src/lumen/harbor.py"), ("x", "thwi.md")]
    queries = ('"thwin"', '"thwi"', '"harbor.py"', '"rwin"')
    fast = Table(rows, trigrams)
    assert [n for n, _ in fast.search('"thwin"', (1.0, 1.0))] == [0]
    assert [n for n, _ in fast.search('"thwi"', (1.0, 1.0))] == [0, 2]
    found = {q: [n for n, _ in fast.search(q, (1.0, 1.0))] for q in queries}
    monkeypatch.setattr(snapshot_fts, "_np", None)
    plain = Table(rows, trigrams)
    for query in queries:
        assert [n for n, _ in plain.search(query, (1.0, 1.0))] == found[query], query


def test_the_walk_as_a_sparse_product_answers_what_the_loop_answers(monkeypatch):
    rnd = random.Random(5)
    names = [f"lumen-{i}" for i in range(300)]
    adj: dict[str, dict[str, float]] = {}
    for _ in range(1200):
        a, b = rnd.sample(names, 2)
        adj.setdefault(a, {})[b] = rnd.choice((0.3, 0.8, 1.0))
    seeds = {names[0]: 1.0, names[7]: 0.5}
    walk = ppr.Walk(damping=0.5, steps=40)
    fast = ppr.run_csr(adj, seeds, walk)
    monkeypatch.setattr(ppr, "_np", None)
    plain = ppr.run_csr(adj, seeds, walk)
    assert fast.keys() == plain.keys() and seeds.keys() <= fast.keys()
    assert [fast[k] for k in plain] == pytest.approx([plain[k] for k in plain], abs=1e-12)
    assert sum(fast.values()) == pytest.approx(1.0)


def test_a_reader_of_any_one_guard_of_an_element_reads_it():
    """Guards are any-of: a reader of either chat a guarded element names reads it, a reader of neither does not."""
    one = '[{"aud": ["chat:41"], "int": "U", "loc": 0, "lvl": 0, "props": {}}'
    other = '{"aud": ["chat:42"], "int": "U", "loc": 0, "lvl": 0, "props": {}}]'
    rows = (
        node("lumen-api:file:a.py", "File", "a.py", {}),
        node("note:shared", "Note", "shared", {}, OPS, f"{one}, {other}"),
    )
    snap = Snapshot.from_stored(StoreRows("sha256:g", rows, (), (), {}, ((0, 0), frozenset())))
    policies = snapshot_masks.of(snap)
    guarded = snap.index["note:shared"]
    for reader, sees in (({"chat:41"}, True), ({"chat:42"}, True), (set(), False)):
        mask = snapshot_masks.Sight(policies, frozenset(reader)).within(None)
        assert bool(mask[policies.node[guarded]]) is sees, reader
        assert mask[policies.node[snap.index["lumen-api:file:a.py"]]]


@pytest.fixture(scope="module")
def small() -> tuple[StoreRows, Snapshot]:
    rows = graph(files=300, per_file=6, calls=4000)
    return rows, Snapshot.from_stored(rows)


def test_a_delta_applied_gives_the_snapshot_a_full_read_gives(small):
    rows, snap = small
    after, delta = changed(rows)
    got = snap.apply_changes(delta)
    assert_same(got, Snapshot.from_stored(after))
    query = "northwind rollout"
    assert (
        SnapshotSearch(got, (1, 0)).search(query)[0]
        == SnapshotSearch(Snapshot.from_stored(after), (1, 0)).search(query)[0]
    )


def test_a_table_updated_holds_what_a_table_of_its_rows_holds():
    rnd = random.Random(9)
    rows = [(text(rnd, 2), f"src/{rnd.choice(WORDS)}.py", text(rnd, 20)) for _ in range(800)]
    table = Table(rows, words)
    kept = [i for i in range(len(rows)) if i % 7]
    after, old = [], []
    for i in kept:
        after.append(rows[i])
        old.append(i)
        if i % 50 == 0:
            after.append(("Northwind", "src/new.py", "a brand new rollout of the lumen ledger"))
            old.append(-1)
    assert_same_table(table.updated(after, old), Table(after, words))


def test_without_numpy_a_delta_and_a_snapshot_answer_the_same(monkeypatch, small):
    rows, snap = small
    after, delta = changed(rows)
    for module in (snapshot_fts, ppr, snapshot_csr, snapshot_carry):
        monkeypatch.setattr(module, "_np", None)
    plain = Snapshot.from_stored(rows)
    assert (plain.src, plain.dst, plain.partitions.keys()) == (snap.src, snap.dst, snap.partitions.keys())
    for key, part in snap.partitions.items():
        assert plain.partitions[key].out_edges == part.out_edges and plain.partitions[key].in_edges == part.in_edges
    got = plain.apply_changes(delta)
    want = Snapshot.from_stored(after)
    assert (got.ids, got.edges, got.names, got.bodies) == (want.ids, want.edges, want.names, want.bodies)
    assert got.text.rows == want.text.rows


@pytest.fixture(scope="module")
def large() -> tuple[StoreRows, Snapshot]:
    """A graph the size of the evo-agents one: some 57,000 nodes and 205,000 edges."""
    rows = graph(files=6300, per_file=8, calls=155_000)
    return rows, Snapshot.from_stored(rows)


QUERIES = (
    "northwind ledger",
    "LumenHarbor",
    "invoice route",
    "mod_420.py",
    "src/harbor",
    "src/quill/mod_1234.py",
    "where is the tariff manifest of a cargo shipment parsed",
    "beacon",
    "OrbitQuill3",
    "ember tally parcel",
    "lumen",
)


def test_kg_search_answers_in_under_25_ms_at_the_95th_percentile_on_a_graph_the_size_of_evo_agents(large):
    _, snap = large
    assert len(snap.nodes) > 50_000 and len(snap.edges) > 200_000
    for reader in (SnapshotSearch(snap, (1, 0)), SnapshotSearch(snap, (0, 0), frozenset({"chat:41"}))):
        timings = []
        for query in QUERIES:
            reader.search(query)  # the first read of a query compiles its reader's masks
            each = []
            for _ in range(5):
                start = time.perf_counter()
                reader.search(query)
                each.append(time.perf_counter() - start)
            timings.append(min(each))  # the query's own cost: the least of the runs, the others slowed by the rest
        p95 = statistics.quantiles(timings, n=20)[-1]
        assert p95 < 0.025, f"kg_search p95 {p95 * 1000:.1f} ms"


def test_a_delta_applies_to_a_snapshot_the_size_of_evo_agents_in_under_500_ms(large):
    rows, snap = large
    after, delta = changed(rows)
    snap.apply_changes(delta)  # the first one reads the indexes a snapshot keeps after it
    took = []
    gc.disable()  # as the hub applies one (``kg_snapshots.uncollected``): no full collection walks the snapshot held
    try:
        for _ in range(3):
            start = time.perf_counter()
            got = snap.apply_changes(delta)
            took.append(time.perf_counter() - start)
    finally:
        gc.enable()
    assert min(took) < 0.5, f"a delta took {min(took) * 1000:.0f} ms"
    assert got.content_hash == after.content_hash and got.ids == tuple(sorted(n[0] for n in after.nodes))
