"""The health of a knowledge graph as the hub models it, without a database: what a build covers and the files of its
git sources (``evo_agents.hub.kg_coverage``, ``evo_agents.kg.health``), the files against a default branch, what the
facets say agents got from the graph, the lens kg_health and its metrics in the ledger, and the retrieval evaluation
of a graph from its own commits (``evo_agents.hub.kg_health_eval``).

Graphs are built from a small harness with a git repo (``tests.kg.projects``) under the test's directory; nothing
touches ~/.evo/kg."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy", reason="the hub's graph health needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_build, kg_coverage, kg_health_eval, ledger, review
from evo_agents.hub.kg_graph import BuiltGraph, GraphCache
from evo_agents.hub.server import kg_health
from evo_agents.kg.build import build_project
from evo_agents.kg.health import is_test_path
from evo_agents.kg.policy import Label
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, make_project, write

SHA = "4f1c2e7a9b3d5f60718293a4b5c6d7e8f9012345"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


# What a build covers


def test_a_builds_coverage_keeps_the_mentions_of_each_source_the_commit_read_and_the_call_reach():
    coverage = {
        "sources": [
            {
                "id": "app",
                "items": 10,
                "mentions": 20,
                "resolved": 15,
                "ambiguous": 3,
                "dangling": 2,
                "samples": {"dangling": ["x.py"]},
                "read": {"commit": SHA, "ref": "origin/main", "base": "origin/main", "behind": 2, "url": "https://x"},
            },
            {"id": "docs", "items": 2, "mentions": 0},
            "not a source",
        ],
        "dangling_edges": 4,
        "warnings": ["ignored"],
    }
    reach = {"functions": 4, "uncalled_functions": 1, "uncalled_function_ratio": 0.25}
    assert kg_coverage.build_coverage(coverage, reach, skipped=True) == {
        "sources": [
            {
                "id": "app",
                "items": 10,
                "mentions": 20,
                "resolved": 15,
                "ambiguous": 3,
                "dangling": 2,
                "resolved_ratio": 0.75,
                "ambiguous_ratio": 0.15,
                "dangling_ratio": 0.1,
                "read": {"commit": SHA, "ref": "origin/main", "base": "origin/main", "behind": 2},
            },
            {
                "id": "docs",
                "items": 2,
                "mentions": 0,
                "resolved": 0,
                "ambiguous": 0,
                "dangling": 0,
                "resolved_ratio": 0.0,
                "ambiguous_ratio": 0.0,
                "dangling_ratio": 0.0,
            },
        ],
        "mentions": 20,
        "resolved": 15,
        "ambiguous": 3,
        "dangling": 2,
        "resolved_ratio": 0.75,
        "ambiguous_ratio": 0.15,
        "dangling_ratio": 0.1,
        "dangling_edges": 4,
        "python": reach,
        "skipped": True,
    }
    assert kg_coverage.build_coverage(None, None, skipped=False)["sources"] == []


@pytest.mark.parametrize(
    "path, test",
    [
        ("tests/hub/test_kg.py", True),
        ("evo_agents/tests/helpers.py", True),
        ("test_x.py", True),
        ("pkg/x_test.py", True),
        ("conftest.py", True),
        ("evo_agents/hub/testing.py", False),
        ("evo_agents/kg/store.py", False),
    ],
)
def test_a_test_path_is_one_of_tests(path, test):
    assert is_test_path(path) is test


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """The demo project of tests.kg.projects (a harness and a git repo app of Python and markdown), synced and built
    in a kg home of the test's own."""
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "home"))
    project = make_project(tmp_path, tmp_path / "home")
    assert all(result.ok for result in sync_project(project))
    report = build_project(project)
    assert report.ok, report.errors
    return SimpleNamespace(
        project=project, report=report, app=tmp_path / "demo-app", graph=project.root / "graph.sqlite"
    )


def test_a_built_graph_says_which_python_methods_no_call_reaches_and_which_files_each_git_source_holds(demo):
    reach = kg_build.graph_reach(demo.graph)
    assert reach == {  # rank_results is called by search, search by Server.handle; nothing calls handle
        "functions": 2,
        "uncalled_functions": 0,
        "uncalled_function_ratio": 0.0,
        "methods": 1,
        "uncalled_methods": 1,
        "uncalled_method_ratio": 1.0,
    }
    head = git(demo.app, "rev-parse", "HEAD").strip()
    tree = dict((line.split("\t")[1], line.split()[2]) for line in git(demo.app, "ls-tree", "-r", "HEAD").splitlines())
    assert kg_coverage.source_files(demo.project) == {"app": {"commit": head, "files": tree, "truncated": False}}
    built = kg_coverage.build_coverage(demo.report.coverage, reach, skipped=demo.report.skipped)
    app = next(source for source in built["sources"] if source["id"] == "app")
    assert app["read"]["commit"] == head and app["items"] >= len(tree) and built["python"] == reach
    assert built["resolved_ratio"] == kg_coverage.ratio(built["resolved"], built["mentions"])


def test_a_git_source_of_more_files_than_a_build_keeps_says_its_list_is_cut(demo, monkeypatch):
    monkeypatch.setattr(kg_coverage, "MAX_FILES", 2)
    found = kg_coverage.source_files(demo.project)["app"]
    assert found["truncated"] is True and list(found["files"]) == ["app/__init__.py", "app/search.py"]


# Against the default branch


def test_files_compare_with_the_default_branch_as_the_source_reads_its_tree():
    tree = {
        "app/a.py": "a1",
        "app/b.py": "b2",
        "app/new.py": "n1",
        "logo.png": "p1",
        "node_modules/x.js": "x1",  # DEFAULT_EXCLUDE
        ".claude/CLAUDE.md": "c1",  # DEFAULT_EXCLUDE, lifted by allow
        "fixtures/big.json": "f1",  # the source's exclude
    }
    held = {"app/a.py": "a1", "app/b.py": "b1", "app/gone.py": "g1", ".claude/CLAUDE.md": "c1"}
    source = {"id": "app", "exclude": ["fixtures/*"], "allow": [".claude/CLAUDE.md"]}
    assert kg_coverage.compare_files(source, tree, held) == {
        "tree_files": 5,
        "graph_files": 4,
        "missing": 2,
        "stale": 1,
        "deleted": 1,
        "samples": {"missing": ["app/new.py", "logo.png"], "stale": ["app/b.py"], "deleted": ["app/gone.py"]},
    }
    selected_only = kg_coverage.compare_files({**source, "assets": False}, tree, held)  # a png is not selected
    assert selected_only["tree_files"] == 4 and selected_only["samples"]["missing"] == ["app/new.py"]
    samples = selected_only["samples"]
    assert kg_coverage.file_evidence("evo-agents", samples) == [
        "code:evo-agents:app/new.py",
        "code:evo-agents:app/b.py",
        "code:evo-agents:app/gone.py",
    ]
    assert kg_coverage.file_evidence(None, samples) == []


def test_the_lag_of_a_graph_is_the_age_of_the_oldest_commit_it_has_not_read():
    assert kg_coverage.lag_hours("2026-10-18T12:00:00Z", NOW) == 24.0
    assert kg_coverage.lag_hours("2026-10-19T11:30:00+00:00", NOW) == 0.5
    assert kg_coverage.lag_hours("2026-10-20T00:00:00Z", NOW) == 0.0  # a clock ahead
    assert kg_coverage.lag_hours(None, NOW) is None and kg_coverage.lag_hours("yesterday", NOW) is None
    assert kg_coverage.lag_hours("2026-10-18T12:00:00", NOW) is None  # no zone: not read


# What agents got from the graph


def kg_body(**numbers) -> dict:
    tools = numbers.pop("tools", [{"tool": "kg_search", "calls": numbers.get("calls", 0), "empty": 0}])
    return {"calls": 0, "empty": 0, "searches": 0, "fallbacks": 0, "edited": 0, "in_results": 0, "in_top": 0} | {
        "tools": tools,
        **numbers,
    }


def test_retrieval_adds_up_what_the_facets_say_of_the_graphs_lookups():
    bodies = [
        ("session:a", kg_body(calls=4, empty=2, searches=3, fallbacks=1, edited=2, in_results=2, in_top=1)),
        (
            "run:7:1",
            kg_body(
                calls=3,
                empty=1,
                fallbacks=1,
                edited=1,
                tools=[{"tool": "kg_search", "calls": 2}, {"tool": "kg_status", "calls": 1}],
            ),
        ),
        ("session:b", kg_body(calls=2, edited=1, in_results=1, in_top=1)),
        ("session:c", None),  # a digest's facet knows nothing of kg
        ("session:d", kg_body()),  # no call of the graph
    ]
    assert kg_coverage.retrieval(bodies) == {
        "used": 3,
        "calls": 9,
        "lookups": 8,  # kg_status names no file: not a lookup
        "empty": 3,
        "searches": 3,
        "fallbacks": 2,
        "edited": 4,
        "in_results": 3,
        "in_top": 2,
        "empty_rate": 0.375,
        "fallback_rate": 0.6667,
        "in_top_rate": 0.5,
        "misses": 2,
        "evidence": ["session:a", "run:7:1"],
    }
    assert kg_coverage.retrieval([])["used"] == 0 and kg_coverage.retrieval([])["empty_rate"] == 0.0


# The lens and the ledger


def health_figures(behind: int, missing: int, empty: int, fallbacks: int, edited: int, in_top: int) -> dict:
    return {
        "sources": [
            {"source": "app", "behind": behind, "missing": missing, "stale": 1, "deleted": 0, "evidence": ["code:a:x"]},
            {"source": "kb", "behind": None, "missing": None, "why": "the hub reads the default branches on GitHub"},
        ],
        "retrieval": {
            "empty": empty,
            "fallbacks": fallbacks,
            "edited": edited,
            "in_top": in_top,
            "misses": edited - in_top,
        },
    }


def night(activity: int, **health) -> dict:
    return {"sessions": {"digests": activity}, "runs": {"done": 0}, "kg_health": health_figures(**health)}


def test_the_lens_kg_health_has_its_figure_and_metrics_in_the_ledger():
    assert list(review.LENSES)[-1] == "kg_health" and "kg_health" in review.FIGURE_KEYS
    assert ledger.LENS_METRICS["kg_health"] == ("kg_behind", "kg_files_off", "kg_empty", "kg_fallbacks", "kg_misses")
    before = night(4, behind=3, missing=2, empty=4, fallbacks=2, edited=4, in_top=1)
    assert [ledger.read_metric(before, key) for key in ledger.LENS_METRICS["kg_health"]] == [3.0, 3.0, 4.0, 2.0, 3.0]
    assert ledger.read_metric({}, "kg_behind") == 0.0 and ledger.read_metric({"kg_health": None}, "kg_misses") == 0.0
    trigger = ledger.trigger_of(before, "kg_health", ["code:a:x"], night="2026-10-18")
    assert [item["key"] for item in trigger["metrics"]] == list(ledger.LENS_METRICS["kg_health"])
    assert trigger["metrics"][0]["what"] == "commits of the default branches the knowledge graph has not read"
    seen = set()
    for offset in range(len(review.LENSES)):
        seen |= set(review.lenses_for(date(2026, 10, 8) + timedelta(days=offset)))
    assert "kg_health" in seen
    prompt = review.build_review_prompt("demo", date(2026, 10, 8), ["kg_health"], [], before)
    assert "- kg_health: the knowledge graph's health" in prompt and '"kg_health"' in prompt


def test_a_level_of_the_graph_compares_as_it_is_and_a_count_per_session_or_run():
    before = night(4, behind=3, missing=2, empty=4, fallbacks=2, edited=4, in_top=1)
    trigger = ledger.trigger_of(before, "kg_health", [])
    busier = night(8, behind=3, missing=2, empty=4, fallbacks=2, edited=4, in_top=1)  # twice the activity
    outcome = ledger.outcome_of(trigger, busier)
    changes = {item["key"]: item["change"] for item in outcome["metrics"]}
    assert changes == {
        "kg_behind": "same",  # still 3 commits behind, however busy the span
        "kg_files_off": "same",
        "kg_empty": "better",  # 4 empty lookups over twice the sessions
        "kg_fallbacks": "better",
        "kg_misses": "better",
    }
    assert outcome["result"] == "keep"
    lagging = night(4, behind=9, missing=8, empty=4, fallbacks=2, edited=4, in_top=1)
    worse = ledger.outcome_of(trigger, lagging)
    assert worse["result"] == "revert" and "commits of the default branches" in worse["reason"]
    assert ledger.worse_evidence(lagging, worse) == ["code:a:x"]
    emptier = night(4, behind=3, missing=2, empty=9, fallbacks=6, edited=4, in_top=1)
    emptier["kg_health"]["retrieval"]["evidence"] = ["session:s1", "run:4:1"]
    assert ledger.worse_evidence(emptier, ledger.outcome_of(trigger, emptier)) == ["session:s1", "run:4:1"]


# The retrieval evaluation


def plan(sha: str, **step) -> dict:
    done = {"id": 1, "title": "Ranking of search results", "repo": "app", "status": "done", "evidence": f"app@{sha}"}
    return {"id": "demo", "goal": "Search.", "repos": [{"repo": "app"}], "steps": [done | step]}


def test_the_eval_asks_the_done_steps_whose_commits_the_graph_read(demo):
    head = git(demo.app, "rev-parse", "HEAD").strip()
    store = Store.open_file(demo.graph)
    try:
        b = store.latest_ready()
        commits = kg_health_eval.GraphCommits(store, b)
        assert commits.changed(head[:10], "app")[1] == 5 and commits.changed(head, "other") is None
        bodies = [
            ("demo", plan(head[:10])),
            ("pending", plan(head, status="pending")),
            ("unread", plan("a1b2c3d4e5")),
            ("no-repo", {"id": "no-repo", "repos": [], "steps": [{"id": 2, "status": "done", "evidence": head}]}),
        ]
        questions, skipped, available = kg_health_eval.plan_questions("demo", bodies, commits)
        assert available == 1 and [q.id for q in questions] == ["demo#1"]
        assert questions[0].files == tuple(
            ("app", path) for path in sorted(kg_coverage.source_files(demo.project)["app"]["files"])
        )
        assert skipped == ["unread#1: none of its 1 commit(s) is in the graph of app", "no-repo#2: no repo named"]
        scored = kg_health_eval.score(questions, skipped, available, Session(demo.project, "agent"))
    finally:
        store.close()
    assert scored["type"] == "plan-step" and scored["questions"] == 1 and scored["skipped"] == 2
    row = scored["methods"]["kg_search"]
    assert row["n"] == 1 and 0.0 <= row["recall@5"] <= 1.0 and row["errors"] == 0
    assert scored["digest"] == kg_health_eval.questions_digest(questions)
    assert kg_health_eval.score([], [], 0, None)["methods"] == {}


def test_a_step_whose_commits_change_too_many_files_asks_nothing(demo):
    for index in range(kg_health_eval.DEFAULT_MAX_FILES + 1):
        write(demo.app / f"many/f{index}.py", f"X = {index}\n")
    commit(demo.app, "many files")
    assert all(result.ok for result in sync_project(demo.project))
    assert build_project(demo.project).ok
    head = git(demo.app, "rev-parse", "HEAD").strip()
    store = Store.open_file(demo.graph)
    try:
        commits = kg_health_eval.GraphCommits(store, store.latest_ready())
        found = kg_health_eval.plan_questions("demo", [("demo", plan(head))], commits)
    finally:
        store.close()
    assert found == ([], ["demo#1: its commits change 21 file(s), not 1 to 20"], 0)


class FileBlobs:
    """A blob store holding one file: what GraphCache fetches an artifact from."""

    def __init__(self, path: Path):
        self.data = path.read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.fetched = 0

    def fetch(self, key, handle, limit):
        self.fetched += 1
        handle.write(self.data)
        return self.sha256, len(self.data)


def test_the_eval_reads_the_hubs_artifact_as_the_owner_through_the_hub_sink_and_keeps_a_score_it_had(
    demo, tmp_path, monkeypatch
):
    staged = tmp_path / "artifact.sqlite"  # one file, out of WAL mode, as the hub's build leaves an artifact
    source, target = sqlite3.connect(f"{demo.graph.resolve().as_uri()}?mode=ro", uri=True), sqlite3.connect(staged)
    source.backup(target)
    source.close()
    target.close()
    copy = Store(staged)
    copy.leave_wal()
    copy.close()
    blobs = FileBlobs(staged)
    built = BuiltGraph(7, blobs.sha256, len(blobs.data), demo.report.content_hash, 1, 1, NOW)
    level = demo.project.policy.levels.index("internal")
    access = SimpleNamespace(
        max_level="internal",
        rules=SimpleNamespace(
            policy=demo.project.policy, ceiling=lambda max_level, sink: Label(level, 0, "U", frozenset())
        ),
    )
    head = git(demo.app, "rev-parse", "HEAD").strip()
    cache = GraphCache(tmp_path / "cache", keep=1)
    plans = [("demo", plan(head))]
    first = kg_health._evaluate(cache, blobs, "demo", access, "agent", [built], plans, None)
    assert first["build_id"] == 7 and first["content_hash"] == built.content_hash and first["questions"] == 1
    assert first["methods"]["kg_search"]["n"] == 1 and blobs.fetched == 1

    def no_score(*args):
        raise AssertionError("the same questions of the same graph are not asked again")

    monkeypatch.setattr(kg_health_eval, "score", no_score)
    again = kg_health._evaluate(cache, blobs, "demo", access, "agent", [built], plans, first)
    assert again == first and blobs.fetched == 1  # the cached artifact, and the score it had


# The command line


def test_the_health_command_prints_a_row_a_source_and_the_score():
    from evo_agents.hub import kg_cli

    source = {"source": "code", "commit": SHA, "behind": 2, "lag_hours": 6.5, "missing": 1, "stale": 0, "deleted": 3}
    assert kg_cli._source_row(source) == ("code", SHA[:12], "2", "6.5", "1", "0", "3", "")
    assert kg_cli._source_row({"source": "kb", "why": "off GitHub"}) == (
        "kb",
        "-",
        "-",
        "-",
        "-",
        "-",
        "-",
        "off GitHub",
    )
    scored = {"build_id": 7, "questions": 12, "methods": {"kg_search": {"recall@5": 0.25, "mrr": 0.4, "empty": 0.0}}}
    assert kg_cli._eval_line(scored) == (
        "retrieval eval of build 7: 12 plan-step questions, kg_search R@5 0.25, MRR 0.4, empty 0.0"
    )
    assert kg_cli._eval_line({"why": "no graph yet"}) == "retrieval eval: no graph yet"
    assert kg_cli._eval_line(None) == "retrieval eval: no question"
