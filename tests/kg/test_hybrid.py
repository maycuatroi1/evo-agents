"""Search by meaning next to BM25 (``evo_agents.kg.semantic``): the vectors a build puts into its rows, the fusion of the two
rankings, and every way it falls back to the search by words. The model is a stand-in that reads words through a
table of synonyms, so a query can match a text by meaning and by no word."""

import json
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="the hub's semantic mode needs the hub-server extra, evo-ak[hub-server]")
np = pytest.importorskip("numpy", reason="the semantic vectors need the semantic extra, evo-ak[semantic]")

from evo_agents.cli import main  # noqa: E402
from evo_agents.hub import kg_semantic  # noqa: E402
from evo_agents.kg import graph_rows, semantic  # noqa: E402
from tests.kg import built  # noqa: E402
from tests.kg.built import rows_of, sync_build, view_of  # noqa: E402
from tests.kg.hub_graph import hub_session  # noqa: E402
from tests.kg.projects import commit, write  # noqa: E402
from tests.kg.test_store import make_searchable  # noqa: E402

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


def kg_search(session, query: str) -> list[str]:
    answer = session.call("kg_search", {"query": query, "limit": 5})
    assert not answer.get("isError"), answer
    return [r["id"] for r in answer["structuredContent"]["results"]]


def found(view, query: str, **kw) -> list[str]:
    return view.search(query, **kw)


def cli(project, on: bool | None = None):
    """The hub's engine for the tests' operator, searching by meaning as ``on`` asks (None: as the graph's mode)."""
    session = hub_session(project, "cli")
    session.semantic = on
    return session


def bare(rows):
    """``rows`` without their vectors."""
    return replace(rows, vectors=(), vector_info=None)


def test_a_project_searches_by_words_alone_until_it_turns_meaning_on(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    assert semantic.read_settings(project.root) == semantic.Settings("off", semantic.DEFAULT_MODEL)
    sync_build(project)
    view = view_of(project)
    assert view.vector_info() is None and rows_of(project).vectors == ()
    assert semantic.query_vector(view, "automobile", True) is None  # no vectors: by words, whatever is asked
    before = {q: found(view, q) for q in ("automobile", "graph builder", "do thi tri thuc")}
    assert CAR not in before["automobile"]
    assert kg_search(cli(project, True), "graph builder") == kg_search(cli(project), "graph builder")
    assert words.calls == 0  # nothing was embedded


def test_the_vectors_sit_outside_the_content_hash(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    off = sync_build(project)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    again = sync_build(project, force=True)
    assert again.content_hash == off.content_hash  # kg/1 and the build's hash do not see the vectors
    rows = rows_of(project)
    held = view_of(project).vector_info()
    assert (held.model, held.dim, held.mode) == (FAKE, DIM, "index")
    keys = {r[0] for r in rows.vectors}
    bodies = sum(1 for _, (_, _, _, _, body, *aud) in rows.keyed if (body or "").strip() and not (aud and aud[0]))
    assert len(keys) >= bodies > 0 and CAR in keys
    assert any("\x1f" in k for k in keys)  # the passages of the long section have theirs
    semantic.write_settings(project.root, semantic.Settings("off", FAKE))
    # a current build keeps its vectors until the next real build; a forced build of the same graph drops them
    sync_build(project, force=True)
    assert view_of(project).vector_info() is None and rows_of(project).vectors == ()


def test_mode_index_ranks_by_words_unless_asked_and_on_fuses_both(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    sync_build(project)
    words.calls = 0
    plain = cli(project)
    by_words = kg_search(plain, "automobile")
    assert CAR not in by_words and words.calls == 0  # index: the query is not even embedded
    plain.semantic = True
    assert kg_search(plain, "automobile")[0] == CAR
    plain.semantic = False
    assert kg_search(plain, "automobile") == by_words

    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project, force=True)
    assert kg_search(cli(project), "automobile")[0] == CAR
    hybrid = kg_search(cli(project), "graph builder")
    module = ("app:file:app/hub_build.py", "symbol:app:app/hub_build.py::GraphBuilder")
    assert hybrid[0] in (*module, "symbol:app:app/hub_build.py::GraphBuilder.buildGraph")  # words still lead


def test_exact_hits_stay_first_and_deprecated_kinds_filters_apply(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project)
    view = view_of(project)
    vector = semantic.query_vector(view, CAR, None)
    assert found(view, CAR, vector=vector)[0] == CAR
    vector = semantic.query_vector(view, "automobile parked", None)
    sections = found(view, "automobile parked", kinds=["Section"], vector=vector)
    assert sections[0] == CAR
    assert {view.node(i)["kind"] for i in sections} == {"Section"}


def test_a_build_embeds_only_the_rows_whose_text_changed(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    sync_build(project)
    first = words.texts
    assert first > 0
    words.texts = 0
    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project, force=True)  # same rows: the mode alone
    assert view_of(project).vector_info().mode == "on" and words.texts == 0
    cache = built.HELD[project.root].cache
    _, again = kg_semantic.index(bare(rows_of(project)), semantic.Settings("on", FAKE), cache)
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
    assert view_of(project).vector_info() is None

    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    sync_build(project, force=True)
    before = kg_search(cli(project, False), "graph builder")
    held = built.HELD[project.root]  # a graph whose model this reader lacks
    held.rows = replace(held.rows, vector_info={**held.rows.vector_info, "model": "fake/missing"})
    assert semantic.query_vector(view_of(project), "automobile", True) is None
    assert CAR not in kg_search(cli(project), "automobile")
    assert kg_search(cli(project), "graph builder") == before


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


def test_kg_semantic_sets_the_mode_on_the_hub_whose_next_build_embeds(tmp_path, kg_env, words, capsys, monkeypatch):
    from evo_agents.kg import settings as kg_settings
    from tests.kg.hub_graph import sign_in

    project = with_garage(tmp_path, kg_env)
    hub = sign_in(monkeypatch, project)
    root = str(project.harness.root)
    assert main(["kg", "semantic", "--project", root, "--mode", "index", "--model", FAKE, "--json"]) == 1
    assert "has no graph on the hub yet" in capsys.readouterr().out  # set, but nothing built yet
    assert ("PUT", "/v1/kg/demo/settings") in hub.asked
    tuning = kg_settings.read(project.root)  # where the hub of the test keeps the project's settings
    assert (tuning.semantic_mode, tuning.semantic_model) == ("index", FAKE)
    sync_build(project)  # the hub's build, which embeds by those settings
    assert main(["kg", "semantic", "--project", root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "demo"
    args = ["kg", "eval", str(FIXTURE), "--project", root, "--method", "kg_search", "--json"]
    assert main(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r["id"]: r["ranks"] for r in data["results"]}["rank"] == [1, 1]  # an identifier stays first


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


def test_each_embed_call_gets_the_models_chunk_so_the_api_backend_fills_its_requests_in_flight(tmp_path, kg_env, words):
    assert semantic.OpenRouterEmbedder.chunk == semantic.OPENROUTER_BATCH * semantic.OPENROUTER_THREADS
    assert semantic.Embedder.chunk == semantic.BATCH * 8
    project = with_garage(tmp_path, kg_env)
    sync_build(project)
    words.chunk, words.calls, words.texts = 2, 0, 0
    _, done = kg_semantic.index(rows_of(project), semantic.Settings("index", FAKE), semantic.MemoryCache())
    assert done.embedded == words.texts > 2 and words.calls == -(-words.texts // 2)


def test_a_spent_budget_leaves_the_rest_for_the_next_build(tmp_path, kg_env, words):
    project = with_garage(tmp_path, kg_env)
    sync_build(project)
    settings = semantic.Settings("index", FAKE)
    cache = semantic.MemoryCache()
    graph = graph_rows.RowsVectors(rows_of(project))
    none = semantic.index(graph, settings, cache, budget=0.0)
    assert none.total > 0 and none.rows == 0 and none.embedded == 0 and none.partial
    assert none.coverage(FAKE) == {
        "model": FAKE,
        "mode": "index",
        "rows": none.total,
        "vectors": 0,
        "partial": True,
    }
    full = semantic.index(graph, settings, cache)  # no budget: the same graph, every row
    assert full.rows == full.total == none.total and not full.partial and full.embedded > 0
    assert full.digest != none.digest
    words.texts = 0
    again = semantic.index(graph, settings, cache, budget=0.0)  # everything held: nothing left to stop
    assert again.rows == again.total and again.embedded == 0 and words.texts == 0
