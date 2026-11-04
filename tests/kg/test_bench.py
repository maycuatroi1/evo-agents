"""`kg bench`: four small sets made here, three small repos made here, nothing downloaded (docs/kg-bench.md)."""

import gc
import json
import os
import sqlite3
import time
from collections import namedtuple
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
Usage = namedtuple("Usage", "total used free")
ROOMY = Usage(4000 * graphs.GB, 0, 2000 * graphs.GB)  # a disk the reserve never stops, whatever this machine has

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
    monkeypatch.setattr(graphs, "disk_usage", lambda path: ROOMY)
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
    # --jobs asks in processes of their own, which read this machine's disk: no reserve
    assert main([*argv, "--out", str(graph), "--out", str(bm25), "--prune", "--jobs", "2", "--reserve-gb", "0"]) == 0
    one, two = compare.load_summary(graph), compare.load_summary(bm25)
    assert (one["settings"]["search"], two["settings"]["search"]) == ("graph", "plain")
    for summary in (one, two):
        assert all(n["build"]["failed"] == 0 for n in summary["benches"].values()), summary["tasks"]
        assert summary["tasks"] and all(r["skipped"] is None for r in summary["tasks"])
    assert sorted((r["bench"], r["id"]) for r in one["tasks"]) == sorted((r["bench"], r["id"]) for r in two["tasks"])
    loc = next(r for r in two["tasks"] if r["bench"] == "locbench")
    assert loc["metrics"]["acc_file@5"] == 1.0  # plain BM25 finds widgets.py by the issue's words too
    built = sum(n["build"]["built"] for n in one["benches"].values())
    assert built == sum(n["build"]["built"] for n in two["benches"].values()) and built >= 2  # pruned, so built again
    lay = graphs.layout(cache)
    assert not any(lay.home.glob("acme__*")) and not any((cache / "repos").glob("*.git"))
    assert not any((lay.base / "memo").glob("*.sqlite"))


def _open_under(root: Path) -> list[str]:
    """The files under ``root`` that an open SQLite connection holds."""
    held = []
    for conn in (o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)):
        try:
            rows = conn.execute("PRAGMA database_list").fetchall()
        except sqlite3.ProgrammingError:  # closed
            continue
        held += [r[2] for r in rows if r[2] and Path(r[2]).resolve().is_relative_to(root.resolve())]
    return held


def test_prune_finds_no_connection_open_on_what_it_deletes(bench, tmp_path, monkeypatch):
    # A connection left open waits for the cycle collector, and Windows cannot delete a file one holds: --prune then
    # left graphs behind (the v0.54.8 and v0.54.9 tag CI).
    cache, mirror, git_url, _ = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    held = []
    prune_graph, prune_repo = graphs.prune_graph, graphs.prune_repo

    def graph(lay, task):
        held.extend(_open_under(lay.project_dir(task)) + _open_under(lay.harness(task)))
        prune_graph(lay, task)

    def repo(lay, name):
        held.extend(_open_under(lay.clone(name)) + _open_under(lay.base / "memo"))
        prune_repo(lay, name)

    monkeypatch.setattr(graphs, "prune_graph", graph)
    monkeypatch.setattr(graphs, "prune_repo", repo)
    argv = ["kg", "bench", "run", "--cache", str(cache), "--git-url", git_url, "--search", "graph", "--search", "plain"]
    outs = ["--out", str(tmp_path / "graph.json"), "--out", str(tmp_path / "plain.json")]
    gc.collect()
    gc.disable()
    try:
        assert main([*argv, *outs, "--prune", "--reserve-gb", "0"]) == 0
    finally:
        gc.enable()
    assert held == []


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


def _three(cache: Path, mirror: Path) -> tuple[tasks.Task, tasks.Task, tasks.Task]:
    """A task on each repo (tune, holdout, test), from the fetched sets."""
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    (mu,), _ = tasks.load_tasks(cache, datasets.MULOCBENCH)
    holdout, test = tasks.load_tasks(cache, datasets.CONTEXTBENCH)[0]
    return mu, holdout, test


def _used(lay: graphs.Layout, task: tasks.Task, ago: float) -> None:
    marker = lay.project_dir(task) / graphs.MARKER
    os.utime(marker, (time.time() - ago, time.time() - ago))


def test_a_build_past_the_cap_evicts_another_extractors_graphs_then_the_least_recently_used(bench):
    cache, mirror, git_url, _ = bench
    one, two, three = _three(cache, mirror)
    lay = graphs.layout(cache)
    free = graphs.Room(cap_gb=None, reserve_gb=0)
    for task in (one, two):
        assert graphs.build_graph(lay, task, git_url=git_url, room=free).error is None
    sizes = [json.loads((lay.project_dir(t) / graphs.MARKER).read_text())["bytes"] for t in (one, two)]
    assert all(size > 0 for size in sizes)  # the marker holds the graph's size
    old = cache / "kg" / "0123456789abcdef"  # an extractor no run builds any more, with a memo
    write(old / "home" / "acme__r0-aaaaaaaaaaaa" / graphs.MARKER, json.dumps({"bytes": 10}))
    write(old / "memo" / "acme__r0.sqlite", "memo")
    gone = time.time() - 2 * graphs.GRACE
    os.utime(old / "home" / "acme__r0-aaaaaaaaaaaa" / graphs.MARKER, (gone, gone))
    _used(lay, one, 3 * graphs.GRACE)  # used before two, so evicted before it
    _used(lay, two, 2 * graphs.GRACE)

    need = sum(sizes) // 2  # the mean graph: what the next build is taken to need
    cap = graphs.Room(cap_gb=(sizes[1] + need + 100) / graphs.GB, reserve_gb=0)
    # the old extractor's graph goes first though used last, then one; two fits under the cap and stays
    assert graphs.build_graph(lay, three, git_url=git_url, room=cap).error is None
    assert not old.exists() and not lay.built(one) and lay.built(two) and lay.built(three)
    assert not lay.harness(one).exists()


def test_a_build_short_of_the_reserve_evicts_what_no_run_uses_or_stops_before_writing(
    bench, monkeypatch, tmp_path, capsys
):
    cache, mirror, git_url, _ = bench
    one, two, _ = _three(cache, mirror)
    lay = graphs.layout(cache)
    assert graphs.build_graph(lay, one, git_url=git_url).error is None
    tight = graphs.Room(cap_gb=None, reserve_gb=10)
    # 5 GB free while one is on the disk, 20 GB once it is gone
    monkeypatch.setattr(
        graphs, "disk_usage", lambda path: Usage(100 * graphs.GB, 0, (5 if lay.built(one) else 20) * graphs.GB)
    )
    with pytest.raises(graphs.DiskFull, match="under the 10.0 GB reserve"):
        graphs.build_graph(lay, two, git_url=git_url, room=tight)  # one was used just now: another run may read it
    _used(lay, one, 2 * graphs.GRACE)
    with pytest.raises(graphs.DiskFull):
        graphs.build_graph(lay, two, git_url=git_url, room=tight, keep=frozenset({lay.name(one)}))
    assert lay.built(one) and not lay.project_dir(two).exists() and not lay.clone(two.repo).exists()  # nothing written

    out = tmp_path / "run.json"
    argv = ["kg", "bench", "run", "contextbench", "--cache", str(cache), "--git-url", git_url, "--out", str(out)]
    assert main([*argv, "--reserve-gb", "30"]) == 1  # one goes, yet 20 GB is short of 30: the run stops, no summary
    assert "reserve" in capsys.readouterr().err and not out.exists()
    assert main([*argv, "--reserve-gb", "10"]) == 0
    assert not lay.built(one) and lay.built(two)


def test_a_marker_without_the_graphs_size_gets_it_and_keeps_its_last_use(bench):
    cache, mirror, git_url, _ = bench
    one, _, _ = _three(cache, mirror)
    lay = graphs.layout(cache)
    assert graphs.build_graph(lay, one, git_url=git_url).error is None
    marker = lay.project_dir(one) / graphs.MARKER
    body = json.loads(marker.read_text())
    marker.write_text(json.dumps({k: v for k, v in body.items() if k != "bytes"}))
    _used(lay, one, 5000)
    before = marker.stat().st_mtime
    (found,) = graphs.cached(lay)
    assert found.bytes == graphs.tree_bytes(lay.project_dir(one)) == json.loads(marker.read_text())["bytes"]
    assert marker.stat().st_mtime == before
    assert graphs.build_graph(lay, one, git_url=git_url).reused and marker.stat().st_mtime > before  # used now


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


def test_whole_file_bm25_ranks_files_by_their_whole_text():
    from evo_agents.kg import filebm25

    index = filebm25.Index({"a.py": "def parse_config(path): read config file", "b.py": "cats " * 50, "c.py": "config"})
    assert index.rank("how to parse the config file", 5) == ["a.py", "c.py"]


def test_like_asks_the_tasks_another_summary_asked_and_the_ranking_digest_follows_the_ranking_code(bench, tmp_path):
    cache, mirror, git_url, _ = bench
    assert main(["kg", "bench", "fetch", "--cache", str(cache), "--from", str(mirror)]) == 0
    first = tmp_path / "first.json"
    argv = ["swe-explore", "--cache", str(cache), "--git-url", git_url, "--seed", "3"]
    assert main(["kg", "bench", "run", *argv, "--out", str(first), "--sample", "1"]) == 0
    ids = json.loads(first.read_text())["benches"]["swe-explore"]["sample"]["ids"]
    selection = run.select(cache, "swe-explore", graphs.layout(cache), run.Options(like=first))
    assert [t.id for t in selection.tasks] == ids and selection.sample["ids"] == ids
    assert selection.sample["why"] == "--like first.json"
    assert len(run.ranking_digest()) == 12 and run.ranking_digest() == run.ranking_digest()


def test_warm_reads_a_graph_once_and_skips_what_is_missing(tmp_path):
    (tmp_path / "graph.sqlite").write_bytes(b"x" * 1000)
    assert graphs.warm(tmp_path) == 1000
    assert graphs.warm(tmp_path / "gone") == 0


def test_contextbench_workspace_prefix_is_stripped_from_answer_paths(monkeypatch):
    gold = [
        {"file": "/workspace/ow__repo__1.2/src/a.c", "start_line": 3, "end_line": 9},
        {"file": "lib/b.py", "start_line": 1, "end_line": 2},
    ]
    row = {
        "instance_id": "x__1",
        "original_inst_id": "x__1",
        "repo": "ow/repo",
        "base_commit": "abc",
        "problem_statement": "it breaks",
        "gold_context": json.dumps(gold),
        "language": "c",
    }
    monkeypatch.setattr(datasets, "_source_rows", lambda cache, bench, source: [row])
    tasks, _ = datasets.read_contextbench(Path("."))
    assert tasks[0]["files"] == ["src/a.c", "lib/b.py"]
    assert sorted(tasks[0]["lines"]) == [["lib/b.py", 1, 2], ["src/a.c", 3, 9]]


def test_bench_profile_reads_the_cache_and_says_so_when_it_is_empty(tmp_path, capsys):
    assert main(["kg", "bench", "profile", "--cache", str(tmp_path / "cache")]) == 0
    assert "no graph with a profile" in capsys.readouterr().out


def test_run_settings_file_asks_under_query_keys_and_refuses_keys_that_need_a_build(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    sync_project(project)
    assert build_project(project).ok
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"settings": {"bm25_name": 3.0, "semantic_weight": 1.0}}))
    out = tmp_path / "tuned.json"
    argv = ["kg", "bench", "run", "--set", f"evo-agents={FIXTURE}", "--project", str(project.harness.root)]
    assert main([*argv, "--settings", str(candidate), "--out", str(out)]) == 0
    assert compare.load_summary(out)["settings"]["kg_settings"] == {"bm25_name": 3.0, "semantic_weight": 1.0}
    plain = tmp_path / "plain.json"
    assert main([*argv, "--out", str(plain)]) == 0
    assert "kg_settings" not in compare.load_summary(plain)["settings"]
    candidate.write_text(json.dumps({"passage_from": 900}))
    assert main([*argv, "--settings", str(candidate), "--out", str(tmp_path / "no.json")]) == 1
    assert "passage_from" in capsys.readouterr().err


# -- the corpus sets (musique, medical, literature, music, customers): one graph a corpus, tiny fixtures ----------------


def _id_in(bench: str, part: str, n: int = 0, prefix: str = "q") -> str:
    """A question id the hash puts in ``part`` of ``bench`` (the nth such)."""
    from evo_agents.kg.bench import corpus

    found = [f"{prefix}{i}" for i in range(400) if corpus.question_part(bench, f"{prefix}{i}") == part]
    return found[n]


def _corpus_mirror(tmp_path, monkeypatch):
    """Four corpus sets in a mirror (JSON Lines, a zip for literature) and their sources: (cache, mirror, ids)."""
    import zipfile

    monkeypatch.setattr(graphs, "disk_usage", lambda path: ROOMY)
    src = {
        datasets.MUSIQUE: (Source("t/musique", "1" * 40, "musique.jsonl"),),
        datasets.MEDICAL: (Source("t/pubmed", "2" * 40, "pqa.jsonl"),),
        datasets.LITERATURE: (Source("t/narrative", "3" * 40, "narrative.zip"),),
        datasets.MUSIC: (
            Source("t/music", "4" * 40, "musicbrainz_genre-0.jsonl"),
            Source("t/music", "4" * 40, "musicbrainz_instrument-0.jsonl"),
            Source("t/music", "4" * 40, "musicbrainz_artist-0.jsonl"),
        ),
        datasets.CUSTOMERS: (),
    }
    monkeypatch.setattr(datasets, "CORPUS_SOURCES", src)
    mirror = tmp_path / "mirror"
    ids = {
        "mu_holdout": _id_in(datasets.MUSIQUE, "holdout", prefix="2hop__"),
        "mu_tune": _id_in(datasets.MUSIQUE, "tune", prefix="3hop__"),
        "med_holdout": _id_in(datasets.MEDICAL, "holdout"),
        "med_tune": _id_in(datasets.MEDICAL, "tune"),
    }

    def paragraph(i, title, text, support):
        return {"idx": i, "title": title, "paragraph_text": text, "is_supporting": support}

    lighthouse = "The Skerry lighthouse keeper logs every ship that passes the northern reef each night."
    harvest = "Barley is harvested in late summer and dried in the long barns of the valley farms."
    glass = "Stained glass windows of the abbey were restored by a guild of glaziers in the fifteenth century."
    _jsonl(
        mirror / "t/musique/musique.jsonl",
        [
            {
                "id": ids["mu_holdout"],
                "question": "Who keeps the lighthouse on the northern reef?",
                "answer": "the keeper",
                "answerable": True,
                "paragraphs": [paragraph(0, "Skerry", lighthouse, True), paragraph(1, "Barley", harvest, False)],
            },
            {
                "id": ids["mu_tune"],
                "question": "Which guild restored the stained glass windows of the abbey?",
                "answer": "glaziers",
                "answerable": True,
                "paragraphs": [paragraph(0, "Abbey", glass, True), paragraph(1, "Barley", harvest, False)],
            },
            {
                "id": "2hop__none",
                "question": "Is anything here?",
                "answer": "",
                "answerable": False,
                "paragraphs": [paragraph(0, "Skerry", lighthouse, True)],
            },
        ],
    )

    def abstract(pubid, question, text):
        context = {"contexts": [text], "labels": ["RESULTS"], "meshes": ["Humans"]}
        return {"pubid": pubid, "question": question, "context": context, "final_decision": "yes"}

    _jsonl(
        mirror / "t/pubmed/pqa.jsonl",
        [
            abstract(ids["med_holdout"], "Does zinc shorten the common cold?", "Zinc lozenges shortened the cold."),
            abstract(ids["med_tune"], "Is aspirin useful after a stroke?", "Aspirin lowered a second stroke."),
            abstract("x1", "Does ginger ease nausea after surgery?", "Ginger eased postoperative nausea."),
        ],
    )
    (mirror / "t/narrative").mkdir(parents=True)
    with zipfile.ZipFile(mirror / "t/narrative/narrative.zip", "w") as zf:
        head = "narrativeqa-master/"
        zf.writestr(
            head + "documents.csv",
            "document_id,set,kind,story_url,story_file_size,wiki_url,wiki_title,story_word_count,story_start,story_end\n"
            "d1,train,gutenberg,u,1,w,The Tide Book,1,a,b\nd2,test,movie,u,1,w,Harbour Lights,1,a,b\n",
        )
        zf.writestr(
            head + "third_party/wikipedia/summaries.csv",
            "document_id,set,summary,summary_tokenized\nd1,train,A fisherman finds a bell under the tide.,x\n"
            "d2,test,A harbour pilot guides a ship through fog.,x\n",
        )
        zf.writestr(
            head + "qaps.csv",
            "document_id,set,question,answer1,answer2,question_tokenized,answer1_tokenized,answer2_tokenized\n"
            "d1,train,What does the fisherman find?,a bell,a bell,q,a,a\n"
            "d2,test,Who guides the ship through fog?,the pilot,a pilot,q,a,a\n",
        )
    skiffle = (
        "Skiffle is a music genre with jazz, blues and folk influences, usually using improvised or homemade "
        "instruments, popular in the 1950s."
    )
    harp = (
        "The Nyckelharpa is a traditional Swedish musical instrument with keys that press tangents against its "
        "strings, played with a bow."
    )
    duo = "Fenn Orchard are a folk duo from the coast who recorded three albums together over a long career."
    music = {
        "genre": [("g1", "Skiffle", skiffle)],
        "instrument": [("i1", "Nyckelharpa", harp)],
        "artist": [("a1", "Fenn Orchard", duo)],
    }
    for kind, rows in music.items():
        _jsonl(
            mirror / f"t/music/musicbrainz_{kind}-0.jsonl",
            [{"mbid": m, "entity": n, "text": t, "url": f"https://x/{n}", "entity_type": kind} for m, n, t in rows],
        )
    return tmp_path / "cache", mirror, ids


def test_corpus_licenses_are_recorded_and_the_noncode_group_names_the_seven():
    from evo_agents.kg.cli_bench import _benches

    seven = ["musique", "longmemeval", "memoryagentbench", "medical", "literature", "music", "customers"]
    assert list(datasets.NONCODE_BENCHES) == seven and _benches(["noncode"]) == seven
    assert set(datasets.LICENSES) == set(seven) and all(datasets.LICENSES.values())
    pinned = [s for b in datasets.CORPUS_BENCHES for s in datasets.CORPUS_SOURCES[b]]
    assert pinned and all(len(s.revision) == 40 for s in pinned)
    assert datasets.CORPUS_SOURCES[datasets.CUSTOMERS] == ()  # generated, nothing downloaded


def test_musique_keeps_questions_with_an_answer_and_a_shared_paragraph_once(tmp_path, monkeypatch):
    from evo_agents.kg.bench import corpus

    cache, mirror, ids = _corpus_mirror(tmp_path, monkeypatch)
    got = corpus.fetch(datasets.MUSIQUE, cache, mirror=mirror)
    assert got["tasks"] == 2 and got["documents"] == 3 and got["skipped"] == {"no answer": 1}
    assert got["license"].startswith("CC-BY-4.0")
    docs, questions = corpus.load(cache, datasets.MUSIQUE)
    by_key = {d["key"]: d for d in docs}
    assert sorted(d["title"] for d in docs) == ["Abbey", "Barley", "Skerry"]  # Barley is in two questions: one document
    answers = {q["id"]: [by_key[k]["title"] for k in q["answers"]] for q in questions}
    assert answers == {ids["mu_holdout"]: ["Skerry"], ids["mu_tune"]: ["Abbey"]}
    assert {q["kind"] for q in questions} == {"2hop", "3hop"}
    assert corpus.question_part("musique", ids["mu_holdout"]) == "holdout"  # the hash of the set and the id


def test_customers_are_invented_the_same_for_a_seed_and_each_question_fits_few_records():
    from evo_agents.kg.bench import corpus

    docs, questions = corpus.generate_customers(120, seed=7)
    assert corpus.generate_customers(120, seed=7) == (docs, questions)
    assert corpus.generate_customers(120, seed=8)[0] != docs
    titles = [d["title"] for d in docs]
    assert len(set(titles)) == len(titles) and all(t.startswith("customers/C-") for t in titles)
    keys = {d["key"] for d in docs}
    assert len(questions) == 120
    for q in questions:
        assert q["id"] in q["answers"] and set(q["answers"]) <= keys and 1 <= len(q["answers"]) <= corpus.MAX_FIT
    assert {q["kind"] for q in questions} == {"ticket", "note", "seats"}
    text = "\n".join(d["text"] for d in docs)
    assert "@" not in text and "http" not in text  # no contact detail, no address
    assert corpus.keep_questions(datasets.CUSTOMERS, questions) == questions  # fewer than MAX_QUESTIONS: all kept
    limit = corpus.MAX_QUESTIONS[datasets.CUSTOMERS]
    many = [{"id": f"x{i}"} for i in range(limit + 50)]
    assert len(corpus.keep_questions(datasets.CUSTOMERS, many)) == limit


def test_corpus_sets_build_one_graph_each_and_score_documents_by_part(tmp_path, monkeypatch, kg_env, capsys):
    cache, mirror, ids = _corpus_mirror(tmp_path, monkeypatch)
    argv = ["--cache", str(cache)]
    sets = ["musique", "medical", "literature", "music", "customers"]
    assert main(["kg", "bench", "fetch", *sets, *argv, "--from", str(mirror)]) == 0
    out = capsys.readouterr().out
    assert "musique: 2 tasks" in out and "literature: 2 tasks" in out and "customers: 800 tasks" in out
    music = datasets.fetched_info(cache, "music")
    assert music["documents"] == 3 and music["files"][0]["revision"] == "4" * 40

    summary_path = tmp_path / "results" / "corpus.json"
    assert main(["kg", "bench", "run", *sets, *argv, "--sample", "40", "--out", str(summary_path)]) == 0
    capsys.readouterr()
    summary = compare.load_summary(summary_path)
    benches = summary["benches"]
    assert list(benches) == sets
    for name in sets:
        bench = benches[name]
        assert bench["kind"] == "corpus" and bench["primary"] == "recall_all@5" and bench["build"]["built"] == 1
        assert bench["license"] and bench["build"]["failed"] == 0 and "test" not in bench["parts"]
    assert benches["musique"]["revision"] == {"t/musique": "1" * 40}
    assert benches["customers"]["revision"]["generator"].startswith("customers-")
    assert benches["medical"]["documents"] == 3 and benches["medical"]["parts"]["holdout"]["n"] >= 1
    held = next(r for r in summary["tasks"] if r["bench"] == "medical" and r["id"] == ids["med_holdout"])
    assert held["metrics"]["recall_all@5"] == 1.0 and held["part"] == "holdout" and held["skipped"] is None
    mu = next(r for r in summary["tasks"] if r["bench"] == "musique" and r["id"] == ids["mu_holdout"])
    assert mu["metrics"]["recall_any@5"] == 1.0
    assert all(r["error"] is None for r in summary["tasks"])
    assert benches["customers"]["sample"]["size"] == 40  # 40 of the customers questions, a sample by the seed
    assert benches["customers"]["parts"]["holdout"]["primary"] is not None

    shown = ["kg", "bench", "show", str(summary_path), "--require", "musique,medical,customers", "--split", "holdout"]
    assert main(shown) == 0
    # a second run reuses the graph of the corpus
    again = tmp_path / "results" / "again.json"
    assert main(["kg", "bench", "run", "medical", *argv, "--out", str(again)]) == 0
    build = compare.load_summary(again)["benches"]["medical"]["build"]
    assert (build["built"], build["reused"], build["failed"]) == (0, 1, 0)


def test_corpus_sets_take_the_graph_search_and_a_limit_kg_search_has(tmp_path, monkeypatch, kg_env, capsys):
    cache, mirror, _ = _corpus_mirror(tmp_path, monkeypatch)
    assert main(["kg", "bench", "fetch", "medical", "--cache", str(cache), "--from", str(mirror)]) == 0
    argv = ["kg", "bench", "run", "medical", "--cache", str(cache)]
    assert main([*argv, "--search", "bm25", "--out", str(tmp_path / "x.json")]) == 1
    assert main([*argv, "--limit", "40", "--out", str(tmp_path / "x.json")]) == 1
    err = capsys.readouterr().err
    assert "--search graph only" in err and "--limit 1 to 25" in err
    assert main(["kg", "bench", "fetch", "nosuchset", "--cache", str(cache)]) == 1
