import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.kg import build
from evo_agents.kg.ids import uuid7
from evo_agents.kg.project import ProjectError, load_project_at, read_index, remember
from evo_agents.kg.sync import due_sources, parse_refresh, sync_all_due, sync_due, sync_project
from tests.kg.conftest import FakeSource, doc
from tests.kg.projects import commit, make_project, write

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)


def stamp(ago: dt.timedelta) -> str:
    return (NOW - ago).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def source(base: Path, sid: str, refresh=None) -> dict:
    fake = FakeSource(base / f"fake-{sid}.json")  # same items for every project, so sharing is harmless
    fake.set(items=[doc("a")])
    spec = {
        "id": sid,
        "connector": "python:tests.kg.fakes:run",
        "config": {"file": str(fake.path)},
        "label": {"level": "internal", "integrity": "U"},
    }
    if refresh is not None:
        spec["refresh"] = refresh
    return spec


def harness(base: Path, name: str, sources: list[dict]) -> Path:
    root = base / f"{name}-harness"
    root.mkdir()
    (root / "harness.yaml").write_text(f"name: {name}\nrepos: []\nknowledge_file: knowledge.yaml\n")
    knowledge = {
        "version": 1,
        "project": name,
        "policy": {
            "levels": ["public", "internal", "customer"],
            "sinks": [{"id": "test-session", "kind": "agent-session", "clearance": {"level": "internal"}}],
        },
        "sources": sources,
    }
    (root / "knowledge.yaml").write_text(yaml.safe_dump(knowledge))
    return root


def add_run(corpus, sid: str, status: str, ago: dt.timedelta) -> None:
    corpus.db.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid7(), sid, "fake", "1", status, stamp(ago), stamp(ago), "{}"),
    )
    corpus.db.commit()


class FakeBuild:
    ok = True
    warnings = ()

    def __init__(self, project):
        self.project = project.name

    def summary_line(self):
        return f"build for {self.project}"

    def to_json(self):
        return {"project": self.project, "ok": self.ok}


@pytest.fixture
def builds(monkeypatch):
    built = []

    def fake_build_project(project, **kwargs):
        built.append(project.name)
        return FakeBuild(project)

    monkeypatch.setattr(build, "build_project", fake_build_project)
    return built


@pytest.mark.parametrize(
    ("value", "seconds"), [("45s", 45), ("30m", 1800), ("1h", 3600), ("6h", 21600), ("1d", 86400), ("90m", 5400)]
)
def test_parse_refresh_due_units(value, seconds):
    assert parse_refresh(value) == seconds


@pytest.mark.parametrize("value", ["", "1", "h", "1.5h", "1w", "0m", "-1h", " 1h", "1h ", "1h\n", "1H", 30, None])
def test_parse_refresh_rejects_malformed_due_values(value):
    with pytest.raises(ValueError, match="like 30m or 6h"):
        parse_refresh(value)


def test_due_sources_with_fake_runs_and_frozen_clock(tmp_path, kg_env):
    hour, minute = dt.timedelta(hours=1), dt.timedelta(minutes=1)
    sources = [
        source(tmp_path, "never-ran", "1h"),
        source(tmp_path, "old-ok", "1h"),
        source(tmp_path, "recent-ok-then-failed", "1h"),
        source(tmp_path, "old-ok-then-failed", "1h"),
        source(tmp_path, "only-failed", "1h"),
        source(tmp_path, "within-slack", "1h"),
        source(tmp_path, "too-recent", "1h"),
        source(tmp_path, "daily", "1d"),
        source(tmp_path, "no-refresh"),
        source(tmp_path, "no-refresh-old"),
    ]
    project = load_project_at(harness(tmp_path, "due", sources), kg_env)
    corpus = project.corpus()
    add_run(corpus, "old-ok", "ok", 2 * hour)
    add_run(corpus, "recent-ok-then-failed", "ok", 10 * minute)
    add_run(corpus, "recent-ok-then-failed", "failed", minute)
    add_run(corpus, "old-ok-then-failed", "ok", 2 * hour)
    add_run(corpus, "old-ok-then-failed", "failed", minute)
    add_run(corpus, "only-failed", "failed", minute)
    add_run(corpus, "within-slack", "ok", 57 * minute)  # an hourly tick lands a little before the hour
    add_run(corpus, "too-recent", "ok", 50 * minute)
    add_run(corpus, "daily", "ok", 20 * hour)
    add_run(corpus, "no-refresh-old", "ok", 30 * 24 * hour)

    assert due_sources(project, corpus, NOW) == [
        "never-ran",
        "old-ok",
        "old-ok-then-failed",
        "only-failed",
        "within-slack",
    ]
    assert due_sources(project, corpus, NOW + 5 * hour) == [
        "never-ran",
        "old-ok",
        "recent-ok-then-failed",
        "old-ok-then-failed",
        "only-failed",
        "within-slack",
        "too-recent",
        "daily",
    ]


def test_malformed_refresh_is_a_project_error_when_due_is_checked(tmp_path, kg_env):
    project = load_project_at(harness(tmp_path, "bad", [source(tmp_path, "docs", "soon")]), kg_env)
    with pytest.raises(ProjectError, match="source 'docs'.*'soon'"):
        due_sources(project, now=NOW)


def test_sync_due_runs_only_due_sources_and_builds_only_when_one_ran(tmp_path, kg_env, builds):
    sources = [source(tmp_path, "hourly", "1h"), source(tmp_path, "manual")]
    project = load_project_at(harness(tmp_path, "solo", sources), kg_env)

    first = sync_due(project, build=True)
    assert first.ok and first.due == ["hourly"]
    assert [r.source for r in first.runs] == ["hourly"]
    assert builds == ["solo"]
    assert project.corpus().runs("manual") == []

    second = sync_due(project, build=True)
    assert second.ok and second.due == [] and second.runs == [] and second.build is None
    assert builds == ["solo"]
    assert len(project.corpus().runs("hourly")) == 1


def test_sync_due_all_one_failing_project_does_not_stop_the_others(tmp_path, kg_env, builds, capsys):
    for name, sources in [
        ("alpha", [source(tmp_path, "docs", "1h")]),
        ("broken", [source(tmp_path, "docs", "soon")]),
        ("idle", [source(tmp_path, "docs")]),
        ("zeta", [source(tmp_path, "docs", "30m"), source(tmp_path, "notes")]),
    ]:
        remember(load_project_at(harness(tmp_path, name, sources), kg_env))
    index = read_index(kg_env)
    index["gone"] = {"harness_root": str(tmp_path / "gone-harness"), "dirs": []}
    (kg_env / "projects.json").write_text(json.dumps(index))

    assert main(["kg", "sync", "--due", "--all", "--build", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    outcomes = {p["project"]: p for p in report["projects"]}
    assert report["ok"] is False
    assert list(outcomes) == ["alpha", "broken", "idle", "zeta"]  # gone has no harness left: skipped
    assert "refresh 'soon'" in outcomes["broken"]["error"] and outcomes["broken"]["ok"] is False
    assert outcomes["alpha"]["ok"] and [r["source"] for r in outcomes["alpha"]["runs"]] == ["docs"]
    assert outcomes["zeta"]["ok"] and outcomes["zeta"]["due"] == ["docs"]
    assert outcomes["idle"]["ok"] and outcomes["idle"]["runs"] == [] and outcomes["idle"]["build"] is None
    assert builds == ["alpha", "zeta"]

    # Nothing is due an instant later, so nothing runs or builds; broken still fails the exit code.
    assert main(["kg", "sync", "--due", "--all", "--build"]) == 1
    out = capsys.readouterr().out
    assert "FAIL project broken:" in out and "project alpha: nothing due" in out
    assert builds == ["alpha", "zeta"]


def test_sync_due_all_succeeds_without_failing_projects(tmp_path, kg_env, builds):
    remember(load_project_at(harness(tmp_path, "alpha", [source(tmp_path, "docs", "1h")]), kg_env))
    outcomes = sync_all_due(kg_env, build=True)
    assert [o.project for o in outcomes] == ["alpha"] and all(o.ok for o in outcomes)
    assert main(["kg", "sync", "--due", "--all"]) == 0


def test_sync_due_flag_combinations_are_rejected(tmp_path, kg_env, capsys):
    assert main(["kg", "sync", "--all"]) == 2
    assert "--all needs --due" in capsys.readouterr().err
    assert main(["kg", "sync", "--due", "--all", "--project", "x"]) == 2
    assert main(["kg", "sync", "--due", "--all", "--accept-removals"]) == 2
    assert main(["kg", "sync", "--due", "--source", "docs"]) == 2


def with_refresh(project):
    """The demo project with its git source due every hour."""
    text = project.knowledge_path.read_text()
    project.knowledge_path.write_text(text.replace("    connector: git\n", "    connector: git\n    refresh: 1h\n"))
    return load_project_at(project.harness.root, project.home)


def test_a_git_source_still_at_the_commit_it_read_writes_no_log_and_builds_nothing(tmp_path, kg_env):
    project = with_refresh(make_project(tmp_path, kg_env))
    app, now = tmp_path / "demo-app", dt.datetime.now(dt.UTC)
    first = sync_due(project, build=True)
    (read,) = first.runs
    assert first.ok and read.source == "app" and not read.unchanged and read.items > 0 and first.build.ok
    corpus = project.corpus()
    logs, cursor = sorted(corpus.log_dir.iterdir()), corpus.cursor("app")

    again = sync_due(project, build=True, now=now + dt.timedelta(hours=2))
    (same,) = again.runs
    assert again.ok and same.ok and same.unchanged and same.items == 0 and same.log is None
    assert again.build is None  # nothing was read, so nothing is built
    assert sorted(corpus.log_dir.iterdir()) == logs and corpus.cursor("app") == cursor
    assert corpus.runs("app", limit=1)[0]["detail"]["unchanged"] is True  # on record: the clock of refresh restarts
    assert due_sources(project, corpus, now + dt.timedelta(minutes=30)) == []

    write(app / "app/search.py", "def search(query):\n    return [query]\n")
    commit(app, "simpler search")
    later = sync_due(project, build=True, now=now + dt.timedelta(hours=4))
    (changed,) = later.runs
    assert not changed.unchanged and changed.items > 0 and changed.log is not None
    assert later.build.ok and not later.build.skipped and later.build.build_id == first.build.build_id + 1


def test_a_git_source_is_read_again_when_its_config_changed_or_its_last_run_failed(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project))
    harness = project.harness.root
    write(harness / "CLUSTER.md", "# Demo\n\nA note not committed anywhere.\n")
    by_source = {r.source: r for r in sync_project(project)}
    assert by_source["app"].unchanged and not by_source["harness"].unchanged
    corpus = project.corpus()
    (cluster,) = [r for r in corpus.items("harness") if r.record["props"].get("path") == "CLUSTER.md"]
    assert "not committed anywhere" in corpus.body_text(cluster.record)  # the harness is read from its files

    add_run(corpus, "app", "failed", dt.timedelta(0))
    (failed_before,) = sync_project(project, ["app"])
    assert failed_before.ok and not failed_before.unchanged and failed_before.items > 0

    excluding = load_project_at(harness, kg_env)
    text = excluding.knowledge_path.read_text()
    excluding.knowledge_path.write_text(text.replace("    repo: app\n", "    repo: app\n    exclude: [docs/old.md]\n"))
    (narrower,) = sync_project(load_project_at(harness, kg_env), ["app"])
    assert not narrower.unchanged and narrower.removals == 1  # docs/old.md is no longer selected

    refresh_only = load_project_at(harness, kg_env)
    text = refresh_only.knowledge_path.read_text()
    refresh_only.knowledge_path.write_text(text.replace("    repo: app\n", "    repo: app\n    refresh: 6h\n"))
    (same,) = sync_project(load_project_at(harness, kg_env), ["app"])
    assert same.unchanged  # when a source runs is not what it reads
