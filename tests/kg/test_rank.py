import json
import math
import re
import tomllib
from pathlib import Path

import pytest

from evo_agents.kg import locate, rank
from evo_agents.kg.bench import rank_train
from evo_agents.kg.bench.rank_train import Group, Label
from evo_agents.kg.build import build_project
from evo_agents.kg.locate import Cand, Pool
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

ROOT = Path(__file__).resolve().parents[2]


def _pool(rows, links=None):
    pool = Pool()
    for node_id, kind, path, rel, line, end in rows:
        pool.cands[node_id] = Cand(node_id, kind, node_id.rsplit(":", 1)[-1], path, line, end, rel)
        pool.order.append(node_id)
        pool.scores[node_id] = 10.0 * rel
    pool.links = links or {}
    return pool


ROWS = [
    ("s:pkg/a.py::load_config", "Symbol", "pkg/a.py", 1.0, 10, 30),
    ("s:pkg/a.py::other", "Symbol", "pkg/a.py", 0.5, 40, 44),
    ("s:pkg/b.py::parse", "Symbol", "pkg/b.py", 0.8, 1, 9),
    ("s:tests/test_b.py::test_parse", "Symbol", "tests/test_b.py", 0.6, 1, 5),
    ("f:docs/guide.md", "File", "docs/guide.md", 0.7, None, None),
]


class Reverse:
    """A model that scores the nodes in the reverse of locate's placed order."""

    def __init__(self):
        self.seen = []

    def predict(self, rows):
        self.seen.append(rows)
        return [row[rank.FEATURES.index("placed")] for row in rows]


def test_features_are_numbers_one_row_per_file_in_order():
    pool = _pool(ROWS, {"s:pkg/a.py::load_config": [("s:pkg/b.py::parse", "calls", 1.0)]})
    pool.cands["s:pkg/a.py::load_config"].anchor = 1.0
    ranked = locate.files(pool)
    rows = rank.features(None, 1, "load_config fails to parse pkg/a.py", pool, ranked)
    assert len(rows) == len(ranked) and all(len(r) == len(rank.FILE_FEATURES) for r in rows)
    assert all(isinstance(v, float) for r in rows for v in r)
    col = {name: i for i, name in enumerate(rank.FEATURES)}
    by_path = {key: rows[k] for k, (_, key, _) in enumerate(ranked)}
    assert by_path["pkg/a.py"][col["anchor_max"]] == 1.0 and by_path["pkg/a.py"][col["anchor_hops"]] == 0.0
    assert by_path["pkg/b.py"][col["anchor_hops"]] == 1.0  # one call from the named symbol
    assert by_path["pkg/b.py"][col["exp_calls"]] > 0 and by_path["pkg/a.py"][col["exp_calls"]] == 0
    assert by_path["tests/test_b.py"][col["is_test"]] == 1.0 and by_path["docs/guide.md"][col["is_doc"]] == 1.0
    assert by_path["pkg/a.py"][col["n_nodes"]] == 2.0 and by_path["pkg/a.py"][col["same_dir"]] == 1.0
    assert by_path["pkg/a.py"][col["sym_cover"]] > 0  # load_config is two of the query's words
    assert math.isnan(by_path["pkg/a.py"][col["cosine"]])  # no embedding asked: missing, not zero
    assert [r[col["loc_rank"]] for r in rows] == [float(k) for k in range(len(rows))]


def test_node_rows_describe_each_node_with_its_files_features_then_its_own():
    pool = _pool(ROWS)
    ranked = locate.files(pool)
    ids, rows = rank.node_rows(None, 1, "load_config fails in pkg/a.py", pool, ranked, limit=5)
    assert sorted(ids) == sorted(i for _, _, nodes in ranked for i in nodes)
    assert all(len(r) == len(rank.FEATURES) and all(isinstance(v, float) for v in r) for r in rows)
    col = {name: i for i, name in enumerate(rank.FEATURES)}
    by_id = dict(zip(ids, rows, strict=True))
    first = by_id["s:pkg/a.py::load_config"]
    assert first[col["node_named"]] == 1.0 and by_id["s:pkg/a.py::other"][col["node_named"]] == 0.0
    assert first[col["node_lines"]] == pytest.approx(math.log1p(21)) and first[col["limit"]] == 5.0
    assert by_id["s:pkg/a.py::other"][col["node_rank"]] == 1.0 and first[col["node_rank"]] == 0.0
    assert first[col["node_in_file"]] == 1.0 > by_id["s:pkg/a.py::other"][col["node_in_file"]]
    assert by_id["f:docs/guide.md"][col["node_whole"]] == 1.0
    assert math.isnan(by_id["f:docs/guide.md"][col["node_lines"]])  # no graph to read the file's length from
    placed = locate.place(ranked)
    assert [i for _, i in sorted((r[col["placed"]], i) for i, r in by_id.items())] == placed


def test_a_ranker_fills_the_first_slots_and_keeps_locates_order_after():
    pool = _pool(ROWS)
    ranked = locate.files(pool)
    placed = locate.place(ranked)
    model = Reverse()
    again = rank.Ranker(model).order(None, 1, "parse config", pool, ranked)
    assert len(model.seen) == 1
    head = list(reversed(placed))[: rank.SLOTS]
    assert again == head + [i for i in placed if i not in head]


def test_the_setting_is_the_callers_then_the_environment_then_the_default(monkeypatch):
    monkeypatch.delenv(rank.ENV, raising=False)
    assert rank.enabled() is rank.DEFAULT_ON
    monkeypatch.setenv(rank.ENV, "on")
    assert rank.enabled() is True and rank.enabled(False) is False
    monkeypatch.setenv(rank.ENV, "off")
    assert rank.enabled() is False and rank.enabled(True) is True
    assert rank.ranker_for(False) is None and rank.state(False) == {"on": False}


def test_no_model_file_or_no_lightgbm_keeps_locates_order(tmp_path, monkeypatch):
    assert rank.load(tmp_path / "missing.txt") is None
    monkeypatch.setattr(rank, "MODEL_FILE", tmp_path / "missing.txt")
    assert rank.ranker_for(True) is None
    assert rank.state(True)["on"] is False


def test_the_shipped_model_is_small_scores_these_features_and_holds_no_words():
    path = rank.MODEL_FILE
    assert path.is_file() and path.stat().st_size <= 5 * 1024 * 1024
    text = path.read_text(encoding="utf-8")
    names = re.search(r"^feature_names=(.*)$", text, re.M)[1].split()
    assert tuple(names) == rank.FEATURES
    words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)) - set(rank.FEATURES)
    # LightGBM's own keys and values only: no path, name or word of a repo the model learned from
    allowed = re.compile(
        r"^(tree|version|v\d+|num_class|num_tree_per_iteration|label_index|max_feature_idx|objective|lambdarank|"
        r"feature_names|feature_infos|tree_sizes|Tree|num_leaves|num_cat|split_feature|split_gain|threshold|"
        r"decision_type|left_child|right_child|leaf_value|leaf_weight|leaf_count|internal_value|internal_weight|"
        r"internal_count|is_linear|shrinkage|end|of|trees|feature_importances|parameters|none|e|inf|nan|"
        r"[a-z_0-9]+)$"
    )
    assert all(allowed.match(w) for w in words), sorted(w for w in words if not allowed.match(w))[:10]


def test_the_shipped_model_loads_when_lightgbm_is_installed():
    pytest.importorskip("lightgbm")
    model = rank.load(rank.MODEL_FILE)
    assert model is not None and model.digest
    pool = _pool(ROWS)
    ranked = locate.files(pool)
    again = model.order(None, 1, "parse config", pool, ranked)
    assert sorted(again) == sorted(locate.place(ranked))


def test_lightgbm_stays_an_extra():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert sorted(meta["dependencies"]) == ["PyYAML>=6.0", "tzdata>=2024.1"]
    assert any(d.startswith("lightgbm") for d in meta["optional-dependencies"]["rank"])
    assert any("rank" in d for d in meta["optional-dependencies"]["hub-server"])


def _code_project(tmp_path, home):
    project = make_project(tmp_path, home)
    app = tmp_path / "demo-app"
    write(app / "app/parser.py", "def parse_config(text):\n    return text.split()\n")
    write(
        app / "app/loader.py",
        "from app.parser import parse_config\n\n\ndef load_config(p):\n    return parse_config(p)\n",
    )
    commit(app, "config")
    sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors
    return project


def test_kg_search_orders_results_by_the_model_only_when_the_setting_is_on(tmp_path, kg_env, monkeypatch):
    project = _code_project(tmp_path, kg_env)
    model = Reverse()
    monkeypatch.setattr(rank, "load", lambda path: rank.Ranker(model, "test"))

    def ids_of(on):
        session = Session(project, "agent")
        session.rank = on
        found = session.call("kg_search", {"query": "parse_config load_config", "limit": 10})["structuredContent"]
        return [r["id"] for r in found["results"]]

    plain = ids_of(False)
    assert not model.seen
    learned = ids_of(True)
    assert model.seen and all(len(r) == len(rank.FEATURES) for r in model.seen[0])
    assert model.seen[0][0][rank.FEATURES.index("limit")] == 10.0
    assert sorted(plain) == sorted(learned) and plain != learned


# -- training ----------------------------------------------------------------------------------------------------------


def test_a_commit_is_a_label_unless_a_bot_made_it_or_it_says_little_or_sweeps():
    held = {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
    good = {
        "author": "Dev d@x",
        "message": "Fix the parser dropping quoted keys (#123)",
        "files": ["pkg/a.py", "uv.lock"],
    }
    text, gold = rank_train.commit_label(good, held, 4, 3)
    assert gold == {"pkg/a.py"} and "#123" not in text
    assert rank_train.commit_label({**good, "author": "dependabot[bot] x@y"}, held, 4, 3) is None
    assert rank_train.commit_label({**good, "message": "fix"}, held, 4, 3) is None
    sweep = {**good, "files": ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py"]}
    assert rank_train.commit_label(sweep, held, 4, 3) is None
    assert rank_train.commit_label({**good, "files": ["pkg/new.py"]}, held, 4, 3) is None  # not in the graph


def test_folds_keep_a_repo_on_one_side_and_training_reads_only_tune_repos(monkeypatch):
    groups = [Group("bench", "s", f"o/r{i % 7}", 1.0, [[0.0]], [1], 1) for i in range(40)]
    found = rank_train.folds(groups, 3)
    assert sum(len(f) for f in found) == 7 and all(not (a & b) for a in found for b in found if a is not b)
    monkeypatch.setattr(rank_train, "repo_split", lambda repo: "tune" if repo == "o/tune" else "holdout")
    labels = [Label("bench", "s", "o/tune", "q", frozenset({"a"})), Label("git", "s", "o/held", "q", frozenset({"a"}))]
    labels.append(Label("plan", "p", "p", "q", frozenset({"a"})))
    assert [lb.repo for lb in rank_train.tune_only(labels)] == ["o/tune", "p"]


def test_click_labels_weigh_a_click_by_the_inverse_propensity_of_its_rank(tmp_path):
    rows = []
    for i in range(40):  # enough labels for the cascade estimate: clicks at rank 1 mostly, at rank 3 sometimes
        click_rank = 3 if i % 4 == 0 else 1
        rows.append(
            {
                "query": f"query {i}",
                "results": [{"id": f"n{k}", "rank": k} for k in range(1, 6)],
                "clicks": [{"id": f"n{click_rank}", "rank": click_rank, "path": f"pkg/f{click_rank}.py"}],
            }
        )
    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    labels = rank_train.click_labels(path, "demo")
    assert len(labels) == 40
    low = next(lb for lb in labels if lb.gold == {"pkg/f1.py"})
    deep = next(lb for lb in labels if lb.gold == {"pkg/f3.py"})
    assert deep.weight > low.weight == 1.0


def test_plan_labels_keep_the_tune_split_only(tmp_path):
    from evo_agents.kg.evaluate import split_of

    lines = [
        {"id": f"plan#{i}", "question": f"step {i}", "type": "plan-step", "files": ["app:app/a.py"]} for i in range(30)
    ]
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    labels = rank_train.plan_labels(path, "demo")
    assert labels and len(labels) == sum(split_of(x["id"]) == "tune" for x in lines)
    assert all(lb.gold == {"app/a.py"} for lb in labels)


def test_a_model_fit_on_rows_learns_which_feature_answers():
    pytest.importorskip("lightgbm")
    import random

    rng = random.Random(1)
    col = rank.FEATURES.index("node_named")
    placed = rank.FEATURES.index("placed")
    groups = []
    for g in range(120):
        rows, labels = [], []
        answer = rng.randrange(8)
        for k in range(8):
            row = [rng.random() for _ in rank.FEATURES]
            row[placed] = float(k)
            row[col] = 1.0 if k == answer else 0.0
            rows.append(row)
            labels.append(4 if k == answer else 0)
        groups.append(Group("bench", "s", f"o/r{g % 10}", 1.0, rows, labels, 1))
    booster = rank_train.fit(groups, 30, {"min_data_in_leaf": 5})
    assert rank_train.importance(booster)[0][0] == "node_named"
    cv = rank_train.evaluate_cv(groups, [30], {"min_data_in_leaf": 5}, k=3)
    assert cv["s"]["30"]["lines"] > cv["s"]["base"]["lines"]


def test_a_nodes_grade_rises_with_the_share_of_its_lines_that_answer():
    label = Label("bench", "s", "o/r", "q", frozenset({"a.py"}), lines=(("a.py", 10, 19),))
    answer = {"a.py": set(range(10, 20))}
    assert rank_train.grade("b.py", 10, 19, label, answer) == 0
    assert rank_train.grade("a.py", 30, 40, label, answer) == 1  # the file, not its lines
    assert rank_train.grade("a.py", 10, 19, label, answer) == 5
    assert rank_train.grade("a.py", 1, 200, label, answer) == 2  # a twentieth of it
    assert rank_train.grade("a.py", 1, 100, label, answer) == 3  # a tenth
    assert rank_train.grade("a.py", None, None, label, answer) == 1
    no_lines = Label("plan", "p", "p", "q", frozenset({"a.py"}))
    assert rank_train.grade("a.py", 10, 19, no_lines, {}) == 1


def test_hunks_are_the_lines_a_commit_replaced_unless_the_file_moved_on(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("".join(f"line {i}\n" for i in range(1, 21)))
    (tmp_path / "b.py").write_text("x\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "b.py").write_text("y\n")
    git("commit", "-qam", "b moves on")
    text = (tmp_path / "a.py").read_text().replace("line 5\n", "five\n").replace("line 6\n", "six\n")
    (tmp_path / "a.py").write_text(text)
    (tmp_path / "b.py").write_text("z\n")
    git("commit", "-qam", "fix")
    sha = git("rev-parse", "HEAD").strip()
    assert rank_train.hunks(tmp_path, sha, base, ["a.py"]) == (("a.py", 5, 6),)
    assert rank_train.hunks(tmp_path, sha, base, ["a.py", "b.py"]) is None
