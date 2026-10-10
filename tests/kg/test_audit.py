"""The audit log: one line per source sync and per build, counts and ids only, mode 0600."""

import json
import sqlite3
import stat

import pytest

from evo_agents import __version__
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project, sync_source
from tests.kg.conftest import doc
from tests.kg.projects import make_project, write

SYNC_KEYS = [
    "v",
    "event",
    "run_id",
    "project",
    "source",
    "status",
    "started_at",
    "finished_at",
    "trigger",
    "evo_agents",
    "connector",
    "connector_version",
    "items",
    "tombstones",
    "removals",
    "rejected",
    "errors",
    "issues",
    "held",
]
BUILD_KEYS = [
    "v",
    "event",
    "build_id",
    "project",
    "status",
    "started_at",
    "finished_at",
    "trigger",
    "evo_agents",
    "content_hash",
    "items",
    "units",
    "nodes",
    "edges",
    "errors",
    "issues",
    "verified",
    "rebuilt_from",
    "exception",
]


@pytest.fixture(autouse=True)
def manual_trigger(monkeypatch):
    monkeypatch.delenv("EVO_KG_TRIGGER", raising=False)


def audit_lines(project, event: str | None = None) -> list[dict]:
    path = project.root / "audit.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if event is None or line["event"] == event]


def test_audit_sync_line_has_counts_and_ids_only(fake_project):
    project, fake = fake_project
    secret = "# confidential-roadmap\n\nPrivate body text that must stay in the corpus.\n"
    fake.set(items=[doc("confidential-roadmap", secret), doc("b")])
    result = sync_source(project, "docs")
    assert result.ok, result.issues

    path = project.root / "audit.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(project.root.stat().st_mode) == 0o700
    [line] = audit_lines(project)
    assert list(line) == SYNC_KEYS
    run = project.corpus().runs("docs", limit=1)[0]
    assert line == {
        "v": 1,
        "event": "sync",
        "run_id": result.run_id,
        "project": "proj",
        "source": "docs",
        "status": "ok",
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "trigger": "manual",
        "evo_agents": __version__,
        "connector": "fake",
        "connector_version": "1",
        "items": 2,
        "tombstones": 0,
        "removals": 0,
        "rejected": 0,
        "errors": 0,
        "issues": 0,
        "held": 0,
    }
    raw = path.read_text(encoding="utf-8")
    for leak in ("confidential", "Private body", "example.test", "docs:doc:"):
        assert leak not in raw


def test_audit_counts_held_removals_and_connector_errors_without_messages(fake_project):
    project, fake = fake_project
    fake.set(items=[doc(k) for k in "abcd"])
    sync_source(project, "docs")
    fake.set(items=[doc("a")], errors=["b"])  # c and d vanish from a complete listing; b failed to fetch
    result = sync_source(project, "docs")
    assert result.ok and result.held, result.issues

    first, second = audit_lines(project)
    assert first["items"] == 4 and first["held"] == 0
    assert second["run_id"] == result.run_id and second["run_id"] > first["run_id"]
    assert (second["status"], second["items"], second["errors"], second["removals"], second["held"]) == (
        "ok",
        1,
        1,
        0,
        2,
    )
    raw = (project.root / "audit.jsonl").read_text(encoding="utf-8")
    assert "timeout" not in raw and "max_removal_ratio" not in raw


def test_audit_failed_sync_records_status_and_issue_count(fake_project):
    project, fake = fake_project
    fake.set(items=[doc(k) for k in "abc"], die_after=1)
    result = sync_source(project, "docs")
    assert not result.ok

    [line] = audit_lines(project)
    assert line["status"] == "failed" and line["items"] == 1
    assert line["issues"] == len(result.issues) > 0
    assert "source went away" not in (project.root / "audit.jsonl").read_text(encoding="utf-8")


def test_audit_sync_without_credentials_still_leaves_a_line(fake_project):
    project, _ = fake_project
    project.knowledge["sources"][0]["credentials"] = ["vendor.token"]
    result = sync_source(project, "docs", credential_getter=lambda key: None)
    assert not result.ok

    [line] = audit_lines(project)
    assert (line["status"], line["connector"], line["connector_version"], line["issues"]) == (
        "failed",
        "python:tests.kg.fakes:run",
        None,
        1,
    )
    assert "vendor.token" not in (project.root / "audit.jsonl").read_text(encoding="utf-8")


def test_audit_trigger_is_schedule_under_the_launch_agent(fake_project, monkeypatch):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    monkeypatch.setenv("EVO_KG_TRIGGER", "schedule")
    sync_source(project, "docs")
    assert build_project(project).ok
    monkeypatch.setenv("EVO_KG_TRIGGER", "something-else")
    sync_source(project, "docs")
    assert [(line["event"], line["trigger"]) for line in audit_lines(project)] == [
        ("sync", "schedule"),
        ("build", "schedule"),
        ("sync", "manual"),
    ]


def test_audit_build_line_matches_the_report(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    results = sync_project(project)
    report = build_project(project)
    assert report.ok, report.errors

    syncs = audit_lines(project, "sync")
    assert sorted(line["run_id"] for line in syncs) == sorted(r.run_id for r in results)
    [line] = audit_lines(project, "build")
    assert list(line) == BUILD_KEYS
    assert line["status"] == "ok"
    assert (line["build_id"], line["content_hash"]) == (report.build_id, report.content_hash)
    assert (line["units"], line["nodes"], line["edges"]) == (report.units, report.nodes, report.edges)
    assert line["items"] == sum(r.items for r in results) > 0
    assert (line["errors"], line["verified"], line["rebuilt_from"], line["exception"]) == (0, None, None, None)
    assert line["started_at"] <= line["finished_at"]
    assert Store.for_project(project).build_row(report.build_id)["content_hash"] == line["content_hash"]


def test_audit_verify_adds_no_line_of_its_own(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_project(project)
    report = build_project(project, verify=True, cold=True)
    assert report.ok and report.verify["match"]
    [line] = audit_lines(project, "build")
    assert line["verified"] is True and line["build_id"] == report.build_id


def test_audit_failed_build_on_a_symbol_key_conflict(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_project(project)
    good = build_project(project)
    source = "  - id: app\n    connector: git\n    repo: app\n"
    text = project.knowledge_path.read_text(encoding="utf-8")
    write(project.knowledge_path, text.replace(source, source + source.replace("id: app", "id: app-copy")))
    project = load_project_at(project.harness.root, project.home)
    sync_project(project)
    bad = build_project(project)
    assert not bad.ok

    first, second = audit_lines(project, "build")
    assert first["build_id"] == good.build_id and first["status"] == "ok"
    assert second["status"] == "failed"
    assert (second["build_id"], second["content_hash"], second["exception"]) == (None, None, None)
    assert second["errors"] == len(bad.errors) > 0
    assert "duplicate" not in (project.root / "audit.jsonl").read_text(encoding="utf-8")


def test_audit_failed_build_on_an_ontology_error(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_project(project)
    write(project.knowledge_path, project.knowledge_path.read_text(encoding="utf-8") + "ontology: missing.yaml\n")
    project = load_project_at(project.harness.root, project.home)
    report = build_project(project)
    assert not report.ok

    [line] = audit_lines(project, "build")
    assert (line["status"], line["build_id"], line["errors"], line["items"]) == ("failed", None, 1, 0)


def test_audit_build_that_raises_leaves_a_failed_line(tmp_path, kg_env, monkeypatch):
    project = make_project(tmp_path, kg_env)
    sync_project(project)

    def broken_write(self, graph, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "write", broken_write)
    with pytest.raises(sqlite3.OperationalError):
        build_project(project)
    [line] = audit_lines(project, "build")
    assert (line["status"], line["build_id"], line["exception"]) == ("failed", None, "OperationalError")


def test_audit_write_failure_warns_and_never_fails_the_run(fake_project, capsys):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    (project.root / "audit.jsonl").mkdir(parents=True)  # a directory where the file should be
    result = sync_source(project, "docs")
    report = build_project(project)
    assert result.ok, result.issues
    assert report.ok, report.errors
    err = capsys.readouterr().err
    assert err.count("warning: could not append to the audit log") == 2

# Trial change for the CI path filter of structure-guardrails; this branch is never merged.
