"""`kg bench`: four small sets made here, three small repos made here, nothing downloaded (docs/kg-bench.md)."""

import json
import os
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.kg.bench import compare, datasets, graphs, run, tasks
from evo_agents.kg.bench.datasets import BenchError, Source
from evo_agents.kg.bench.locbench_repos import LOCBENCH_REPOS
from evo_agents.kg.build import build_project
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, make_project, write

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "demo.jsonl"
TUNE_REPO, HOLDOUT_REPO, TEST_REPO = "acme/r0", "acme/r7", "acme/r4"  # their parts by the hash of the name

WIDGETS = '''"""Widgets and their config."""


def parse_config(text):
    """Parse the config text into a dict of settings, one key=value a line."""
    settings = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        settings[key.strip()] = value.strip()
    return settings


def render_widget(name, settings):
    """Render the widget called name with its settings."""
    return f"<{name} {settings}>"
'''

ISSUE = "parse_config crashes on a config text with an empty line: the settings come back wrong"


def test_parts_come_from_the_repo_name_and_no_locbench_repo_is_in_tune(monkeypatch):
    assert [tasks.repo_split(r) for r in (TUNE_REPO, HOLDOUT_REPO, TEST_REPO)] == ["tune", "holdout", "test"]
    assert tasks.repo_split("ACME/R0 ") == "tune"  # the name, lowercased: the same in every set and machine
    assert all(tasks.repo_split(name) != "tune" for name in LOCBENCH_REPOS)
    assert len(LOCBENCH_REPOS) == 165
    monkeypatch.setattr(tasks, "LOCBENCH_REPOS", frozenset({TUNE_REPO}))
    assert tasks.repo_split(TUNE_REPO) == "holdout"
    names = [f"acme/x{i}" for i in range(400)]
    shares = {p: sum(tasks.repo_split(n) == p for n in names) / len(names) for p in tasks.PARTS}
    assert 0.4 < shares["tune"] < 0.6 and 0.15 < shares["holdout"] < 0.35 and 0.15 < shares["test"] < 0.35


def test_patch_answers_are_the_old_lines_a_patch_changes():
    patch = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -3,4 +3,4 @@ def f():
 keep
-old
+new
 keep
+added
diff --git a/pkg/new.py b/pkg/new.py
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1 @@
+x = 1
"""
    files, lines = datasets.patch_answers(patch)
    assert files == ["pkg/a.py"]
    assert lines == [("pkg/a.py", 4, 4), ("pkg/a.py", 4, 4), ("pkg/a.py", 5, 5)]
    assert datasets.merge_ranges(lines) == [["pkg/a.py", 4, 5]]


def test_a_commit_time_with_an_offset_no_clock_has_is_read_in_utc():
    from evo_agents.kg.connectors._git import iso_time

    assert iso_time("2011-09-08T02:38:50+518:00\n") == "2011-08-17T12:38:50Z"  # a real commit of psf/requests
    assert iso_time("2011-09-08T02:38:50-05:30") == "2011-09-08T02:38:50-05:30"


def test_the_cache_is_never_inside_a_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    with pytest.raises(BenchError, match="outside any repo"):
        datasets.cache_dir(repo / "cache")
    monkeypatch.setenv(datasets.CACHE_ENV, str(tmp_path / "cache"))
    assert datasets.cache_dir() == (tmp_path / "cache").resolve()


def _task(repo: str, n: int) -> tasks.Task:
    return tasks.Task("locbench", f"{repo}-{n}", f"{repo}-{n}", repo, "c" * 40, "python", "q", ("a.py",))


def test_a_sample_takes_a_share_of_every_repo_and_the_same_seed_the_same_tasks():
    pool = [_task("acme/big", i) for i in range(60)] + [_task("acme/mid", i) for i in range(30)]
    pool += [_task("acme/one", 0)]
    picked = tasks.sample(pool, 30, seed=7)
    assert len(picked) == 30 and picked == tasks.sample(pool, 30, seed=7)
    by_repo = {r: sum(t.repo == r for t in picked) for r in ("acme/big", "acme/mid", "acme/one")}
    assert by_repo["acme/one"] == 1 and 19 <= by_repo["acme/big"] <= 20 and 9 <= by_repo["acme/mid"] <= 10
    assert tasks.sample(pool, 30, seed=8) != picked
    assert tasks.sample(pool, 500, seed=7) == pool


# -- four small sets on three small repos --------------------------------------------------------------------------


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def bench(tmp_path, monkeypatch, kg_env):
    """Three repos of one commit each, the four sets about them in a mirror, and the cache: (cache, mirror, shas)."""
    src = tmp_path / "src"
    shas = {}
    for repo in (TUNE_REPO, HOLDOUT_REPO, TEST_REPO):
        path = src / repo
        write(path / "widgets.py", WIDGETS)
        write(path / "README.md", f"# {repo}\n\nWidgets.\n")
        git(path, "init", "-q", "-b", "main")
        commit(path, "init")
        shas[repo] = git(path, "rev-parse", "HEAD").strip()

    sources = {
        datasets.CONTEXTBENCH: (Source("t/cb", "1" * 40, "cb.jsonl"),),
        datasets.SWE_EXPLORE: (
            Source("t/explore", "2" * 40, "explore.jsonl"),
            Source("t/verified", "3" * 40, "verified.jsonl"),
            Source("t/pro", "4" * 40, "pro.jsonl"),
            Source("t/multi", "5" * 40, "multi.jsonl"),
        ),
        datasets.LOCBENCH: (Source("t/loc", "6" * 40, "loc.jsonl"),),
        datasets.MULOCBENCH: (Source("t/muloc", "7" * 40, "muloc.jsonl"),),
    }
    monkeypatch.setattr(datasets, "SOURCES", sources)
    explore = sources[datasets.SWE_EXPLORE]
    monkeypatch.setattr(
        datasets, "EXPLORE_SOURCES", {"verified": explore[1], "pro": explore[2], "multilingual": explore[3]}
    )
    monkeypatch.setattr(datasets, "LOCBENCH_REPOS", frozenset({HOLDOUT_REPO}))

    mirror = tmp_path / "mirror"
    region = {"path": "widgets.py", "start": 4, "end": 10}
    gold = json.dumps([{"file": "widgets.py", "start_line": 4, "end_line": 10, "content": ""}])
    issue = {"problem_statement": ISSUE}
    _jsonl(
        mirror / "t/cb/cb.jsonl",
        [
            # acme__r0-1 is SWE-Explore's too: kept there, left out here
            {
                "instance_id": f"cb-{i}",
                "original_inst_id": f"{r.replace('/', '__')}-{i}",
                "repo": r,
                "base_commit": shas[r],
                "gold_context": gold,
                "language": "python",
                **issue,
            }
            for i, r in ((1, TUNE_REPO), (2, HOLDOUT_REPO), (3, TEST_REPO))
        ],
    )
    _jsonl(
        mirror / "t/explore/explore.jsonl",
        [
            {
                "instance_id": f"{r.replace('/', '__')}-1",
                "dataset": "verified",
                "ground_truth": {"read_core_files": ["widgets.py"], "read_core_regions": [region]},
            }
            for r in (TUNE_REPO, HOLDOUT_REPO)
        ]
        + [{"instance_id": "acme__gone-1", "dataset": "verified", "ground_truth": {}}],
    )
    _jsonl(
        mirror / "t/verified/verified.jsonl",
        [
            {"instance_id": f"{r.replace('/', '__')}-1", "repo": r, "base_commit": shas[r], **issue}
            for r in (TUNE_REPO, HOLDOUT_REPO)
        ],
    )
    _jsonl(mirror / "t/pro/pro.jsonl", [])
    _jsonl(mirror / "t/multi/multi.jsonl", [])
    patch = (
        "diff --git a/widgets.py b/widgets.py\n--- a/widgets.py\n+++ b/widgets.py\n@@ -8,3 +8,4 @@\n"
        '         key, _, value = line.partition("=")\n+        if not key:\n+            continue\n'
        "         settings[key.strip()] = value.strip()\n"
    )
    _jsonl(
        mirror / "t/loc/loc.jsonl",
        [
            {
                "repo": HOLDOUT_REPO,
                "instance_id": "acme__r7-9",
                "base_commit": shas[HOLDOUT_REPO],
                "patch": patch,
                "edit_functions": ["widgets.py:parse_config"],
                **issue,
            }
        ],
    )
    loc = {
        "base_commit": shas[TUNE_REPO],
        "files": [
            {
                "path": "widgets.py",
                "status": "modified",
                "Loc": {"(None, 'parse_config', 4)": {"mod": [8, 9], "add": []}},
            },
            {"path": "extra.py", "status": "added", "Loc": {}},
            {"path": "README.md", "status": "modified", "Loc": [2]},  # a few real rows give the lines alone
        ],
    }
    _jsonl(
        mirror / "t/muloc/muloc.jsonl",
        [
            {
                "organization": "acme",
                "repo_name": "r0",
                "base_commit": shas[TUNE_REPO],
                "iss_html_url": "https://github.com/acme/r0/issues/5",
                "pr_html_url": "https://github.com/acme/r0/pull/6",
                "title": "parse_config and empty lines",
                "body": ISSUE,
                "file_loc": str(loc),
                "loctype": {},
            }
        ],
    )
    cache = tmp_path / "cache"
    git_url = f"{src}/{{repo}}"
    return cache, mirror, git_url, shas


def test_fetch_pins_each_file_and_keeps_an_instance_two_sets_share_once(bench, capsys):
    cache, mirror, _, shas = bench
    with pytest.raises(BenchError, match="fetch swe-explore"):
        datasets.fetch(datasets.CONTEXTBENCH, cache, mirror=mirror) and tasks.load_tasks(cache, "contextbench")
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    out = capsys.readouterr().out
    assert "swe-explore: 2 tasks" in out and "1 not in SWE-bench verified" in out

    info = datasets.fetched_info(cache, datasets.SWE_EXPLORE)
    assert [f["revision"] for f in info["files"]] == ["2" * 40, "3" * 40, "4" * 40, "5" * 40]
    assert all(len(f["sha256"]) == 64 for f in info["files"])

    cb, dropped = tasks.load_tasks(cache, datasets.CONTEXTBENCH)
    assert dropped == 1 and [t.id for t in cb] == ["cb-2", "cb-3"]
    q = cb[0].as_question()
    assert q.text == ISSUE and q.lines == ((None, "widgets.py", 4, 10),) and q.files == ((None, "widgets.py"),)

    (loc,), _ = tasks.load_tasks(cache, datasets.LOCBENCH)
    assert loc.nodes == ("widgets.py::parse_config",) and loc.lines == (("widgets.py", 8, 8),)  # inserted after 8
    assert loc.as_question().nodes == ("symbol:repo:widgets.py::parse_config",)
    (mu,), _ = tasks.load_tasks(cache, datasets.MULOCBENCH)
    assert mu.files == ("widgets.py", "README.md") and mu.commit == shas[TUNE_REPO]
    assert mu.lines == (("README.md", 2, 2), ("widgets.py", 8, 9))
    assert mu.question.startswith("parse_config and empty lines\n\n")


def test_run_builds_each_graph_once_apart_from_the_hub_and_writes_a_summary(bench, kg_env, tmp_path, capsys):
    cache, mirror, git_url, shas = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    argv = ["--cache", str(cache), "--git-url", git_url]
    assert main(["kg", "bench", "build", "locbench", *argv]) == 0
    assert "1 built, 0 reused" in capsys.readouterr().out

    out = tmp_path / "results" / "run.json"
    assert main(["kg", "bench", "run", *argv, "--out", str(out)]) == 0
    assert os.environ["EVO_KG_HOME"] == str(kg_env) and not kg_env.exists()  # the hub's kg home: never touched
    summary = compare.load_summary(out)
    assert summary["evo_ak"]["version"] and summary["extractor"] == graphs.extractor()
    assert summary["settings"]["seed"] == 0 and summary["settings"]["sealed"] is False
    benches = summary["benches"]
    assert list(benches) == list(datasets.CODE_BENCHES)
    assert benches["swe-explore"]["revision"]["t/explore"] == "2" * 40 and benches["swe-explore"]["limit"] == 5
    assert {b: n["primary"] for b, n in benches.items()} == {
        "contextbench": "line_f1",
        "swe-explore": "line_f1",
        "locbench": "acc_file@5",
        "mulocbench": "acc_file@5",
    }
    assert all("test" not in n["parts"] for n in benches.values())
    assert benches["contextbench"]["parts"]["holdout"]["n"] == 1 and benches["contextbench"]["deduped"] == 1
    assert benches["locbench"]["parts"]["tune"]["n"] == 0  # its one repo is never in tune
    assert benches["mulocbench"]["parts"]["tune"]["n"] == 1
    # two graphs (tune and holdout repo, one commit each); built once, by build or the first set that needs it
    assert sum(n["build"]["built"] for n in benches.values()) == 1
    assert all(n["build"]["failed"] == 0 for n in benches.values())
    for record in summary["tasks"]:
        assert record["skipped"] is None and record["error"] is None, record
        assert record["part"] in ("tune", "holdout") and record["method"] == "kg_search"
    loc = next(r for r in summary["tasks"] if r["bench"] == "locbench")
    assert loc["metrics"]["acc_file@5"] == 1.0 and loc["metrics"]["hit@5"] == 1.0  # widgets.py is found
    assert benches["locbench"]["parts"]["holdout"]["languages"]["python"]["n"] == 1

    assert main(["kg", "bench", "build", *argv]) == 0
    assert "0 built" in capsys.readouterr().out.splitlines()[-1]
    assert (
        main(["kg", "bench", "show", str(out), "--require", ",".join(datasets.CODE_BENCHES), "--split", "holdout"]) == 1
    )
    assert "FAIL  require: mulocbench: 0 tasks in holdout" in capsys.readouterr().out
    assert (
        main(
            [
                "kg",
                "bench",
                "show",
                str(out),
                "--require",
                "contextbench,locbench",
                "--split",
                "holdout",
                "--cost-max",
                "0",
                "--precision-min",
                "0",
            ]
        )
        == 0
    )


def test_one_pass_asks_each_search_on_the_same_graphs_and_prunes_them(bench, tmp_path):
    cache, mirror, git_url, _ = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    graph, bm25 = tmp_path / "graph.json", tmp_path / "plain.json"
    argv = ["kg", "bench", "run", "--cache", str(cache), "--git-url", git_url, "--search", "graph", "--search", "plain"]
    assert main([*argv, "--out", str(graph)]) == 1  # one --out a search
    assert main([*argv, "--out", str(graph), "--out", str(bm25), "--prune", "--jobs", "2"]) == 0
    one, two = compare.load_summary(graph), compare.load_summary(bm25)
    assert (one["settings"]["search"], two["settings"]["search"]) == ("graph", "plain")
    for summary in (one, two):
        assert all(n["build"]["failed"] == 0 for n in summary["benches"].values())
        assert summary["tasks"] and all(r["skipped"] is None for r in summary["tasks"])
    assert sorted((r["bench"], r["id"]) for r in one["tasks"]) == sorted((r["bench"], r["id"]) for r in two["tasks"])
    loc = next(r for r in two["tasks"] if r["bench"] == "locbench")
    assert loc["metrics"]["acc_file@5"] == 1.0  # plain BM25 finds widgets.py by the issue's words too
    built = sum(n["build"]["built"] for n in one["benches"].values())
    assert built == sum(n["build"]["built"] for n in two["benches"].values()) and built >= 2  # pruned, so built again
    lay = graphs.layout(cache)
    assert not any(lay.home.glob("acme__*")) and not any((cache / "repos").glob("*.git"))
    assert not any((lay.base / "memo").glob("*.sqlite"))


def test_a_run_goes_on_from_the_repos_a_dead_run_finished(bench, tmp_path):
    cache, mirror, git_url, _ = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    first, again = tmp_path / "first.json", tmp_path / "again.json"
    argv = ["kg", "bench", "run", "--cache", str(cache), "--search", "graph", "--prune"]
    assert main([*argv, "--git-url", git_url, "--out", str(first)]) == 0
    # the clones and graphs are gone and the remote too: only the kept answers can give the same run
    gone = "file:///nonexistent/{repo}.git"
    assert main([*argv, "--git-url", gone, "--out", str(again)]) == 0
    one, two = compare.load_summary(first), compare.load_summary(again)
    assert two["tasks"] and all(r["skipped"] is None for r in two["tasks"])
    assert [(r["id"], r["metrics"]) for r in one["tasks"]] == [(r["id"], r["metrics"]) for r in two["tasks"]]


def test_run_reads_the_sealed_test_part_only_with_sealed(bench, tmp_path, capsys):
    cache, mirror, git_url, _ = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    argv = ["contextbench", "--cache", str(cache), "--git-url", git_url, "--out", str(tmp_path / "s.json")]
    assert main(["kg", "bench", "run", *argv, "--part", "test"]) == 1
    assert "sealed" in capsys.readouterr().err
    assert not (tmp_path / "s.json").exists()
    assert main(["kg", "bench", "run", *argv, "--sealed"]) == 0
    summary = json.loads((tmp_path / "s.json").read_text())
    assert summary["benches"]["contextbench"]["parts"]["test"]["n"] == 1
    assert main(["kg", "bench", "show", str(tmp_path / "s.json")]) == 1
    assert "the test part is sealed" in capsys.readouterr().out
    assert main(["kg", "bench", "show", str(tmp_path / "s.json"), "--sealed"]) == 0


def test_run_samples_a_set_with_a_seed_and_names_the_sample(bench, tmp_path):
    cache, mirror, git_url, _ = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    out = tmp_path / "s.json"
    argv = ["swe-explore", "--cache", str(cache), "--git-url", git_url, "--out", str(out), "--seed", "3"]
    assert main(["kg", "bench", "run", *argv, "--sample", "1"]) == 0
    sample = json.loads(out.read_text())["benches"]["swe-explore"]["sample"]
    assert (sample["size"], sample["of"], sample["seed"], len(sample["ids"])) == (1, 2, 3, 1)

    # past max-hours at the build time measured so far, a sample of the share that fits
    lay = graphs.layout(cache)
    with (lay.base / "builds.jsonl").open("a") as f:
        f.write(json.dumps({"bench": "swe-explore", "seconds": 7200.0}) + "\n")
    selection = run.select(cache, "swe-explore", lay, run.Options(max_hours=1.0, seed=3))
    assert selection.sample is not None and selection.sample["size"] == 1 and "over 1 h" in selection.sample["why"]


def test_run_scores_a_local_question_set_such_as_evo_agents_in_tune_and_holdout(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    sync_project(project)
    assert build_project(project).ok
    out = tmp_path / "local.json"
    argv = ["kg", "bench", "run", "--set", f"evo-agents={FIXTURE}", "--project", str(project.harness.root)]
    assert main([*argv, "--out", str(out)]) == 0
    summary = compare.load_summary(out)
    part = summary["benches"]["evo-agents"]
    assert part["primary"] == "hit@5" and set(part["parts"]) == {"tune", "holdout"}
    assert sum(p["n"] for p in part["parts"].values()) == part["tasks"] > 0
    assert main(["kg", "bench", "show", str(out), "--require", "evo-agents"]) == 0


# -- compare -------------------------------------------------------------------------------------------------------


def _summary(values: dict[str, list[float]], tokens: float = 100.0, ms: float = 10.0, part: str = "holdout") -> dict:
    benches, records = {}, []
    for bench, scores in values.items():
        metric = run.PRIMARY.get(bench, run.LOCAL_PRIMARY)
        benches[bench] = {"primary": metric, "method": "kg_search", "parts": {part: {"n": len(scores)}}}
        records += [
            {
                "bench": bench,
                "id": f"{bench}-{i}",
                "part": part,
                "method": "kg_search",
                "metrics": {metric: v},
                "tokens": tokens,
                "ms": ms,
                "skipped": None,
            }
            for i, v in enumerate(scores)
        ]
    return {"format": run.FORMAT, "benches": benches, "tasks": records}


def _write(path: Path, summary: dict) -> str:
    path.write_text(json.dumps(summary))
    return str(path)


SCORES = [0.2, 0.4, 0.6, 0.8, 0.5, 0.3, 0.7, 0.9, 0.1, 0.5] * 3


def test_compare_gate_fails_when_a_set_drops_by_over_two_standard_errors(tmp_path, capsys):
    before = _summary({"contextbench": SCORES, "locbench": SCORES})
    worse = _summary({"contextbench": [max(v - 0.2, 0) for v in SCORES], "locbench": SCORES})
    b, a = _write(tmp_path / "b.json", before), _write(tmp_path / "a.json", worse)
    assert main(["kg", "bench", "compare", b, a, "--split", "holdout", "--gate"]) == 1
    out = capsys.readouterr().out
    assert "regressed" in out and "FAIL  gate: dropped by over 2 standard errors: contextbench" in out
    assert main(["kg", "bench", "compare", b, a, "--split", "holdout"]) == 0  # without --gate it only reports


def test_compare_gate_passes_noise_and_pairs_only_shared_tasks(tmp_path):
    noise = [v + (0.01 if i % 2 else -0.01) for i, v in enumerate(SCORES)]
    before = _summary({"contextbench": SCORES, "swe-explore": SCORES})
    after = _summary({"contextbench": noise + [0.0] * 5, "mulocbench": SCORES})
    verdict = compare.compare(before, after, gate=True)
    (row,) = verdict.rows
    assert row.bench == "contextbench" and row.n == len(SCORES) and verdict.ok
    assert compare.compare(before, after, split="tune").rows[0].n == 0


def test_compare_min_targets_needs_that_many_sets_improved_beyond_the_interval(tmp_path, capsys):
    before = _summary({"contextbench": SCORES, "locbench": SCORES, "mulocbench": SCORES})
    after = _summary({"contextbench": [v + 0.1 for v in SCORES], "locbench": SCORES, "mulocbench": SCORES})
    b, a = _write(tmp_path / "b.json", before), _write(tmp_path / "a.json", after)
    target = ["--target", "contextbench,locbench"]
    assert main(["kg", "bench", "compare", b, a, "--gate", *target, "--min-targets", "1"]) == 0
    assert main(["kg", "bench", "compare", b, a, "--gate", *target, "--min-targets", "2"]) == 1
    assert "1 of contextbench, locbench improved" in capsys.readouterr().out
    assert main(["kg", "bench", "compare", b, a, *target]) == 1  # default: every target


def test_compare_p95_ms_and_tokens_drop(tmp_path):
    before = _summary({"locbench": SCORES}, tokens=1000.0, ms=50.0)
    after = _summary({"locbench": SCORES}, tokens=700.0, ms=120.0)
    assert compare.compare(before, after, p95_ms=150).ok
    assert not compare.compare(before, after, p95_ms=100).ok
    assert compare.compare(before, after, tokens_drop=0.3).ok
    assert not compare.compare(before, after, tokens_drop=0.35).ok
    b, a = _write(tmp_path / "b.json", before), _write(tmp_path / "a.json", after)
    assert main(["kg", "bench", "compare", b, a, "--tokens-drop", "0.35", "--json"]) == 1


def test_compare_refuses_what_is_not_a_summary_and_sets_scored_differently(tmp_path, capsys):
    other = _write(tmp_path / "x.json", {"rows": []})
    good = _write(tmp_path / "g.json", _summary({"locbench": SCORES}))
    assert main(["kg", "bench", "compare", other, good]) == 2
    assert "not a bench summary" in capsys.readouterr().err
    changed = _summary({"locbench": SCORES})
    changed["benches"]["locbench"]["primary"] = "line_f1"
    with pytest.raises(BenchError, match="score it on"):
        compare.compare(_summary({"locbench": SCORES}), changed)


def test_show_checks_the_thresholds_later_steps_read(tmp_path, capsys):
    summary = _summary({"locbench": SCORES})
    summary["benches"]["locbench"]["parts"]["holdout"].update(abstain=0.8, false_abstain=0.1, precision=0.4)
    summary.update(arms=["graph", "bm25"], cost={"usd": 1.5})
    path = _write(tmp_path / "s.json", summary)
    base = ["kg", "bench", "show", path, "--require", "locbench", "--split", "holdout"]
    ok = [
        "--abstain-min",
        "0.7",
        "--false-abstain-max",
        "0.2",
        "--precision-min",
        "0.3",
        "--arms",
        "graph,bm25",
        "--cost-max",
        "2",
    ]
    assert main([*base, *ok]) == 0
    for flag, value in (
        ("--abstain-min", "0.9"),
        ("--false-abstain-max", "0.05"),
        ("--precision-min", "0.5"),
        ("--arms", "hybrid"),
        ("--cost-max", "1"),
    ):
        assert main([*base, flag, value]) == 1, flag
    capsys.readouterr()
    assert main(["kg", "bench", "show", path, "--precision-min", "0.1"]) == 1
    assert "give --split" in capsys.readouterr().out


def test_plain_bm25_ors_every_word_of_the_issue_but_stop_words():
    from evo_agents.kg import search

    assert (
        search.plain_query("The parse_config crashes; parse_config again, x 2")
        == '"parse" OR "config" OR "crashes" OR "again" OR "2"'
    )
    assert search.plain_query("the a of") is None


def test_merge_adds_the_sets_and_names_where_hybrid_vectors_came_from(tmp_path):
    def summary(bench):
        return {
            "format": "evo-kg-bench/1",
            "extractor": "x",
            "settings": {"search": "hybrid"},
            "cost": {"usd": 0.0, "model_calls": 0},
            "benches": {bench: {"parts": {}}},
            "tasks": [{"bench": bench, "id": "1"}],
        }

    ledger = tmp_path / "spend.jsonl"
    ledger.write_text('{"tokens": 100, "usd": 0.5, "provider": "Parasail"}\n{"tokens": 50, "usd": 0.25}\n')
    merged = compare.merge([summary("a"), summary("b")], ledger)
    assert list(merged["benches"]) == ["a", "b"] and len(merged["tasks"]) == 2
    assert merged["settings"]["embeddings"] == {
        "backend": "openrouter",
        "model": "baai/bge-m3",
        "providers": ["Parasail"],
        "tokens": 150,
    }
    assert merged["cost"]["usd"] == 0.75
    with pytest.raises(BenchError, match="two summaries"):
        compare.merge([summary("a"), summary("a")])
    other = summary("c")
    other["extractor"] = "y"
    with pytest.raises(BenchError, match="extractor"):
        compare.merge([summary("a"), other])
