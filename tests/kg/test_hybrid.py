"""Search by meaning next to BM25 (``evo_agents.kg.semantic``): the vectors a build writes, the fusion of the two
rankings, and every way it falls back to the search by words. The model is a stand-in that reads words through a
table of synonyms, so a query can match a text by meaning and by no word."""

import json
import sqlite3
import zlib
from pathlib import Path

import numpy as np
import pytest

from evo_agents.cli import main
from evo_agents.kg import semantic
from evo_agents.kg.serve import Session
from evo_agents.kg.store import SCHEMA_VERSION, Store
from tests.kg.projects import commit, write
from tests.kg.test_store import found, make_searchable, sync_build

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "demo.jsonl"
FAKE = "fake/words"
DIM = 64
SYNONYMS = {"automobile": "car", "vehicle": "car", "motorcar": "car"}
CAR = "app:file:docs/garage.md#garage"


class Words(semantic.Embedder):
    """A vector of the words of a text, each word put to one of DIM axes, synonyms to the same one."""

    name, dim = FAKE, DIM

    def __init__(self):
        self.calls = 0
        self.texts = 0

    def embed(self, texts):
        self.calls += 1
        self.texts += len(texts)
        out = []
        for text in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for word in semantic.query_text(text).lower().replace("#", " ").split():
                word = SYNONYMS.get(word.strip(".,:;"), word.strip(".,:;"))
                v[zlib.crc32(word.encode()) % DIM] += 1.0
            norm = np.linalg.norm(v)
            out.append(v / norm if norm else v)
        return out


@pytest.fixture
def words(monkeypatch):
    model = Words()
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, lambda: model)
    yield model
    semantic.forget_models()


def with_garage(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/garage.md", "# Garage\n\nThe car is parked here every night.\n")
    commit(app, "garage")
    return project


def kg_search(session: Session, query: str) -> list[str]:
    answer = session.call("kg_search", {"query": query, "limit": 5})
    assert not answer.get("isError"), answer
    return [r["id"] for r in answer["structuredContent"]["results"]]


def test_a_project_searches_by_words_alone_until_it_turns_meaning_on(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    assert semantic.read_settings(project.root) == semantic.Settings("off", semantic.DEFAULT_MODEL)
    sync_build(project)
    store = Store.for_project(project)
    assert store.vector_info() is None and not semantic.has_vectors(store.db)
    assert semantic.query_vector(store, "automobile", True) is None  # no vectors: by words, whatever is asked
    before = {q: found(store, q) for q in ("automobile", "graph builder", "do thi tri thuc")}
    assert CAR not in before["automobile"]
    session = Session(project, "cli")
    session.semantic = True
    assert kg_search(session, "graph builder") == kg_search(Session(project, "cli"), "graph builder")
    assert words.calls == 0  # nothing was embedded


def test_the_vectors_sit_outside_the_schema_and_the_content_hash(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    off = sync_build(project)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    again = sync_build(project, force=True)
    assert again.content_hash == off.content_hash  # kg/1 and the build's hash do not see the vectors
    path = project.root / "graph.sqlite"
    reader = Store.open_file(path)  # a reader of before opens the file as it did
    assert Store.stored_schema(path) == SCHEMA_VERSION
    held = reader.vector_info()
    assert (held.model, held.dim, held.mode) == (FAKE, DIM, "index")
    keys = {k for (k,) in reader.db.execute("SELECT key FROM node_vec")}
    bodies = reader.db.execute(
        "SELECT count(*) FROM node_fts_rows r JOIN node_fts f ON f.rowid = r.fts_rowid"
        " WHERE r.aud = 0 AND length(trim(f.body)) > 0"
    ).fetchone()[0]
    assert len(keys) >= bodies > 0 and CAR in keys
    assert any("\x1f" in k for k in keys)  # the passages of the long section have theirs
    semantic.write_settings(project.root, semantic.Settings("off", FAKE))
    sync_build(project)  # the build is current; off drops the vectors all the same
    assert Store.open_file(path).vector_info() is None


def test_mode_index_ranks_by_words_unless_asked_and_on_fuses_both(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    sync_build(project)
    words.calls = 0
    plain = Session(project, "cli")
    by_words = kg_search(plain, "automobile")
    assert CAR not in by_words and words.calls == 0  # index: the query is not even embedded
    plain.semantic = True
    assert kg_search(plain, "automobile")[0] == CAR
    plain.semantic = False
    assert kg_search(plain, "automobile") == by_words

    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project)
    assert kg_search(Session(project, "cli"), "automobile")[0] == CAR
    hybrid = kg_search(Session(project, "cli"), "graph builder")
    assert hybrid[0] in ("app:file:app/hub_build.py", "symbol:app:app/hub_build.py::GraphBuilder")  # words still lead


def test_exact_hits_stay_first_and_deprecated_kinds_filters_apply(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project)
    store = Store.for_project(project)
    vector = semantic.query_vector(store, CAR, None)
    assert found(store, CAR, vector=vector)[0] == CAR
    vector = semantic.query_vector(store, "automobile parked", None)
    sections = found(store, "automobile parked", kinds=["Section"], vector=vector)
    assert sections[0] == CAR
    kinds = dict(store.db.execute("SELECT node_id, kind FROM node_fts"))
    assert {kinds[i] for i in sections} == {"Section"}


def test_a_build_embeds_only_the_rows_whose_text_changed(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    sync_build(project)
    first = words.texts
    assert first > 0 and (project.root / semantic.CACHE_FILE).exists()
    path = project.root / "graph.sqlite"
    words.texts = 0
    assert semantic.index_file(path, semantic.Settings("on", FAKE)).embedded == 0  # same rows: the mode alone
    assert Store.open_file(path).vector_info().mode == "on" and words.texts == 0
    db = sqlite3.connect(path)
    try:
        with db:
            semantic.drop(db)
    finally:
        db.close()
    again = semantic.index_file(path, semantic.Settings("on", FAKE))
    assert again.embedded == 0 and again.rows > 0 and words.texts == 0  # every vector from the cache
    app = tmp_path / "demo-app"
    write(app / "docs/garage.md", "# Garage\n\nThe motorcar sleeps here.\n")
    commit(app, "garage again")
    sync_build(project)
    assert 0 < words.texts < first


def test_without_the_model_the_build_warns_and_search_stays_by_words(tmp_path, kg_env, words, monkeypatch):
    project = with_garage(tmp_path, kg_env)

    def missing():
        raise semantic.SemanticUnavailable("fastembed is not installed")

    monkeypatch.setitem(semantic.FACTORIES, "fake/missing", missing)
    semantic.write_settings(project.root, semantic.Settings("on", "fake/missing"))
    report = sync_build(project)
    assert report.ok and any("no vectors written" in w and "fastembed" in w for w in report.warnings)
    store = Store.for_project(project)
    assert store.vector_info() is None

    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project)
    by_words = Session(project, "cli")
    by_words.semantic = False
    before = kg_search(by_words, "graph builder")
    db = sqlite3.connect(project.root / "graph.sqlite")
    try:
        with db:  # a graph whose model this reader lacks
            db.execute("UPDATE node_vec_meta SET value = 'fake/missing' WHERE key = 'model'")
    finally:
        db.close()
    assert semantic.query_vector(Store.for_project(project), "automobile", True) is None
    assert CAR not in kg_search(Session(project, "cli"), "automobile")
    assert kg_search(Session(project, "cli"), "graph builder") == before


def test_nearest_keeps_a_node_at_its_best_row_and_rows_of_an_audience_to_its_readers():
    m = semantic.Matrix(
        ["a", "a", "b", "c"],
        ["Section", "Section", "File", "Section"],
        [None, None, None, '["team"]'],
        np.array([[1, 0], [0.9, 0.1], [0.8, 0.2], [0.95, 0]], dtype=np.float32),
    )
    assert semantic.nearest(m, [1, 0], 5, [], lambda aud: False) == [("a", 1.0), ("b", pytest.approx(0.8))]
    assert [n for n, _ in semantic.nearest(m, [1, 0], 5, [], lambda aud: True)] == ["a", "c", "b"]
    assert [n for n, _ in semantic.nearest(m, [1, 0], 5, ["File"], lambda aud: True)] == ["b"]
    assert [n for n, _ in semantic.nearest(m, [1, 0], 1, [], lambda aud: True)] == ["a"]


def test_fuse_adds_the_weighted_reciprocal_ranks():
    assert semantic.fuse([(1.0, ["a", "b", "c"]), (1.0, ["c", "d"])], k=1) == ["c", "a", "b", "d"]
    assert semantic.fuse([(1.0, ["a", "b"]), (0.0, ["b"])], k=60) == ["a", "b"]
    assert semantic.fuse([(1.0, []), (1.0, ["x", "y"])]) == ["x", "y"]


def test_settings_are_read_and_checked(tmp_path):
    assert semantic.read_settings(tmp_path) == semantic.Settings()
    semantic.write_settings(tmp_path, semantic.Settings("on", FAKE))
    assert semantic.read_settings(tmp_path) == semantic.Settings("on", FAKE)
    (tmp_path / semantic.SETTINGS_FILE).write_text('{"mode": "sometimes"}', encoding="utf-8")
    with pytest.raises(ValueError, match="sometimes"):
        semantic.read_settings(tmp_path)


def test_eval_runs_both_ways_on_one_build(tmp_path, kg_env, words, capsys):
    project = with_garage(tmp_path, kg_env)
    root = str(project.harness.root)
    assert main(["kg", "semantic", "--project", root, "--mode", "index", "--model", FAKE, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "index"
    sync_build(project)
    assert main(["kg", "semantic", "--project", root]) == 0
    assert f"vectors of {FAKE}, mode index" in capsys.readouterr().out
    runs = {}
    for search in ("bm25", "hybrid", "graph"):
        args = ["kg", "eval", str(FIXTURE), "--project", root, "--method", "kg_search", "--search", search, "--json"]
        assert main(args) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["search"] == search
        runs[search] = {r["id"]: r["ranks"] for r in data["results"]}
    assert runs["graph"] == runs["bm25"]  # mode index: the graph ranks by words
    assert runs["hybrid"]["rank"] == [1, 1]  # an identifier stays first by its exact hit


# -- the opt-in API backend ----------------------------------------------------------------------------------------


def test_openrouter_backend_is_opt_in_unit_length_and_keeps_a_ledger(tmp_path, monkeypatch):
    from evo_agents.kg import semantic

    ledger = tmp_path / "spend.jsonl"
    monkeypatch.setenv("EVO_KG_EMBED_LEDGER", str(ledger))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    semantic.forget_models()

    def fake_post(self, texts):
        data = [{"index": i, "embedding": [3.0, 4.0] + [0.0] * 1022} for i in reversed(range(len(texts)))]
        return {"data": data, "provider": "P", "usage": {"total_tokens": 1_000_000}}

    monkeypatch.setattr(semantic.OpenRouterEmbedder, "_post", fake_post)
    monkeypatch.delenv(semantic.BACKEND_ENV, raising=False)
    assert type(semantic.OpenRouterEmbedder("BAAI/bge-m3")).__name__ == "OpenRouterEmbedder"
    monkeypatch.setenv(semantic.BACKEND_ENV, "openrouter")
    model = semantic.embedder("BAAI/bge-m3")
    assert isinstance(model, semantic.OpenRouterEmbedder)
    vectors = model.embed(["a", "b"])
    assert len(vectors) == 2 and abs(float(vectors[0][0]) - 0.6) < 1e-6
    assert semantic.spent_usd() == pytest.approx(0.01) and model.provider == "P"
    monkeypatch.setattr(semantic, "OPENROUTER_MAX_USD", 0.005)
    with pytest.raises(semantic.SemanticUnavailable, match="spend reached"):
        model.embed(["c"])
    semantic.forget_models()
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(semantic.SemanticUnavailable, match="OPENROUTER_API_KEY"):
        semantic.embedder("BAAI/bge-m3")
    semantic.forget_models()
