"""Old copies of secrets in a kg home (``evo_agents.kg.scrub``, ``evo-agents kg scrub``).

A project synced before its git connector redacted secrets keeps them: in the blobs of the first run, which the graph
was built from, and the graph's full-text index. Once a redacting sync has replaced them, the old blobs hold the only
copies. ``kg scrub`` counts them by default and changes nothing; with ``--purge`` it deletes the old blobs and the run
logs of old runs naming them, never what the corpus still uses, and compacts the memo and the graph so no file of the
project holds a value any more. Nothing it prints, answers or appends to the audit log holds a value either."""

import builtins
import gc
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.kg import scrub as scrub_module
from evo_agents.kg.build import build_project, memo_path
from evo_agents.kg.connectors import git as git_connector
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.memo import Memo
from evo_agents.kg.redact import redact_file
from evo_agents.kg.scrub import (
    KEPT,
    LIVE,
    OLD,
    ScrubBusy,
    holding_sources,
    might_hold_secret,
    project_dirs,
    purge,
    run_summary,
    scan_project,
    secrets_in_json,
)
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc
from tests.kg.projects import make_project
from tests.kg.test_redact import (
    COMPOSE,
    COMPOSE_PASSWORD,
    GITHUB,
    LITERAL,
    PEM,
    REDACTED,
    SECRETS,
    SERVICE_ACCOUNT,
    SETTINGS,
    URI_PASSWORD,
    add_secrets,
)

ALL_SECRETS = (*SECRETS, LITERAL)


def leaked(text: str) -> list[str]:
    return [secret for secret in ALL_SECRETS if secret in text]


def files_holding(root: Path, secrets=ALL_SECRETS) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and any(secret.encode() in path.read_bytes() for secret in secrets)
    )


def snapshot(root: Path) -> dict[str, str]:
    """Every file of ``root`` by hash, once the SQLite files hold their WAL: connections a test left open checkpoint
    as they are collected, which is no change of content. The shared-memory index of a WAL is left out."""
    gc.collect()
    for path in root.glob("*.sqlite"):
        db = sqlite3.connect(path)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith("-shm")
    }


def kg(*args: str, capsys) -> tuple[int, str, str]:
    code = main(["kg", "scrub", *args])
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture
def synced_before_redaction(tmp_path, kg_env, monkeypatch):
    """A project synced and built while its git connector did not redact, then synced and built again with redaction
    on: the first run's blobs are the old copies."""
    project = make_project(tmp_path, kg_env)
    add_secrets(tmp_path / "demo-app")
    with monkeypatch.context() as patched:
        patched.setattr(git_connector, "redact_file", lambda text, path: (text, {}))
        assert all(r.ok for r in sync_project(project))
        assert build_project(project).ok
    assert files_holding(project.root)  # the control: the values are on disk
    return project


def resync(project) -> str:
    assert all(r.ok for r in sync_project(project))
    report = build_project(project)
    assert report.ok, report.errors
    return report.content_hash


# Reading


def test_scrub_finds_live_copies_before_a_redacting_sync_and_old_ones_after(synced_before_redaction):
    project = synced_before_redaction
    before = scan_project(project.name, project.root)
    assert before.found(OLD) == [] and before.logs == []
    live = before.found(LIVE)
    paths = {path for finding in live for path in finding.paths}
    assert {"docker-compose.yml", "config/service-account.json", "app/settings.py"} <= paths
    assert {"password", "uri_password", "pem", "github_token", "aws_key_id", "google_api_key"} <= set(before.secrets())
    assert before.graph_rows > 0  # the graph's full-text index holds the text

    resync(project)
    after = scan_project(project.name, project.root)
    assert after.found(LIVE) == [] and after.found(KEPT) == []
    old = after.found(OLD)
    assert {finding.sha256 for finding in old} == {finding.sha256 for finding in live}
    compose = next(f for f in old if f.paths == ["docker-compose.yml"])
    assert compose.secrets == {"password": 1, "uri_password": 1}
    assert compose.items == ["app:file:docker-compose.yml"]
    first_app_run = sorted((project.root / "log").glob("*.jsonl.gz"))
    assert after.logs and set(after.logs) < {path.name for path in first_app_run}
    assert after.graph_rows == 0  # the build rewrote the index from the redacted corpus
    assert not leaked(json.dumps(after.to_json()))


def test_scrub_counts_by_default_and_changes_no_file(synced_before_redaction, capsys):
    project = synced_before_redaction
    resync(project)
    before = snapshot(project.root)

    code, out, err = kg("--project", project.name, capsys=capsys)
    assert code == 0, err
    assert not leaked(out + err)
    assert f"{project.name}: " in out and "docker-compose.yml" in out and "password 1, uri_password 1" in out
    assert "Count only, nothing was deleted. --purge deletes" in out

    code, out, err = kg("--project", project.name, "--json", capsys=capsys)
    assert code == 0, err
    body = json.loads(out)
    assert not leaked(out + err)
    assert body["purge"] is False and body["totals"][OLD] >= 3 and body["totals"]["logs"] >= 1
    (found,) = body["projects"]
    assert found["deleted"] is None and found["counts"][LIVE] == 0
    assert all(set(blob) == {"sha256", "size", "state", "secrets", "paths", "items"} for blob in found["blobs"])
    assert snapshot(project.root) == before


def test_scrub_purge_needs_yes_without_a_terminal_and_asks_on_one(synced_before_redaction, capsys, monkeypatch):
    project = synced_before_redaction
    resync(project)
    before = snapshot(project.root)

    code, out, err = kg("--project", project.name, "--purge", capsys=capsys)
    assert code == 2 and "--yes" in err and out == ""
    code, out, err = kg("--project", project.name, "--purge", "--json", capsys=capsys)
    assert code == 2 and "--yes" in err

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asked = []
    monkeypatch.setattr(builtins, "input", lambda prompt: asked.append(prompt) or "n")
    code, out, err = kg("--project", project.name, "--purge", capsys=capsys)
    assert code == 1 and asked == ["Delete them? [y/N] "]
    assert "About to delete" in out and "This cannot be undone." in out and "Nothing was deleted." in out
    assert not leaked(out + err)
    assert snapshot(project.root) == before


# Purging


def test_scrub_purge_deletes_old_copies_and_keeps_what_the_corpus_uses(synced_before_redaction, capsys):
    project = synced_before_redaction
    content = resync(project)
    found = scan_project(project.name, project.root)
    old = {finding.sha256 for finding in found.found(OLD)}
    corpus = Corpus(project.name, project.home)
    state = corpus.state_digest()
    corpus.close()

    code, out, err = kg("--project", project.name, "--purge", "--yes", capsys=capsys)
    assert code == 0, err
    assert not leaked(out + err)
    assert f"About to delete {len(old)} old blob(s)" in out
    assert f"{project.name}: deleted {len(old)} blob(s)" in out and "memo and graph compacted" in out

    assert files_holding(project.root) == []  # blobs gone, the graph's free pages too
    for sha in old:
        assert not (project.root / "blobs" / "sha256" / sha[:2] / sha[2:]).exists()
    assert not any((project.root / "log" / name).exists() for name in found.logs)
    corpus = Corpus(project.name, project.home)
    try:
        assert corpus.state_digest() == state  # nothing the corpus uses went
        assert corpus.state_digest(corpus.rebuild_state()) == state  # the logs left still replay to it
    finally:
        corpus.close()
    rebuilt = build_project(project)
    assert rebuilt.ok and rebuilt.content_hash == content
    session = Session(project, "agent")
    assert session.call("kg_search", {"query": "POSTGRES_PASSWORD"})["structuredContent"]["results"]
    for secret in (COMPOSE_PASSWORD, URI_PASSWORD, GITHUB):
        assert session.call("kg_search", {"query": secret})["structuredContent"]["results"] == []

    lines = [json.loads(line) for line in (project.root / "audit.jsonl").read_text().splitlines()]
    (line,) = [line for line in lines if line["event"] == "scrub"]
    assert line["blobs"] == len(old) and line["logs"] == len(found.logs) and line["live"] == 0
    assert line["secrets"] == found.secrets() and not leaked(json.dumps(line))

    again = scan_project(project.name, project.root)
    assert again.clean and again.found(OLD) == [] and again.logs == []


def test_scrub_purge_keeps_live_copies_until_a_sync_replaces_them(synced_before_redaction, capsys):
    project = synced_before_redaction
    code, out, err = kg("--project", project.name, "--purge", "--yes", capsys=capsys)
    assert code == 0, err
    assert "Nothing to delete; compacting the memo and the graph." in out
    assert not leaked(out + err)
    after = scan_project(project.name, project.root)
    assert after.found(LIVE) and after.found(OLD) == []
    assert build_project(project).ok  # every blob the corpus uses is there


def test_scrub_keeps_an_old_blob_a_run_in_use_still_names(fake_project):
    project, fake = fake_project
    secret = f"DB_PASSWORD: {LITERAL}\n"
    fake.set(items=[doc("a", f"# A\n\n```\n{secret}```\n", rev="1"), doc("b", rev="1")])
    sync_project(project)
    fake.set(items=[doc("a", "# A\n\nThe password is in the vault.\n", rev="2")], listing=False)  # b not listed
    sync_project(project)

    found = scan_project(project.name, project.root)
    assert found.blobs and found.found(OLD) == [] and found.found(LIVE) == []
    assert {(f.state, tuple(f.items), tuple(f.secrets.items())) for f in found.blobs} == {
        (KEPT, ("docs:doc:a",), (("password", 1),))
    }
    assert found.logs == []  # the first run still holds b's record
    purge(found)
    assert {f.sha256 for f in found.blobs} <= {
        p.parent.name + p.name for p in (project.root / "blobs" / "sha256").glob("*/*")
    }


def test_scrub_purge_deletes_memo_rows_holding_a_secret(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a", rev="1")])
    sync_project(project)
    assert build_project(project).ok
    memo = Memo(memo_path(project))
    memo.run("probe", "1", {"x": "1"}, lambda: {"text": f"POSTGRES_PASSWORD={COMPOSE_PASSWORD}"})
    memo.close()

    found = scan_project(project.name, project.root)
    assert len(found.memo) == 1 and found.memo_secrets == {"password": 1} and found.blobs == []
    purge(found)
    assert found.deleted["memo"] == 1
    assert scan_project(project.name, project.root).memo == []
    assert build_project(project).ok


def test_scrub_purge_waits_for_a_running_sync(synced_before_redaction, capsys):
    project = synced_before_redaction
    resync(project)
    with holding_sources(project.root):  # as a sync holds its source's lock
        with pytest.raises(ScrubBusy):
            with holding_sources(project.root):
                pass
        code, out, err = kg("--project", project.name, "--purge", "--yes", capsys=capsys)
    assert code == 1 and "a sync of source" in err and "is running" in err
    assert scan_project(project.name, project.root).found(OLD)


def test_scrub_all_reads_every_project_directory_of_the_kg_home(synced_before_redaction, kg_env, capsys):
    project = synced_before_redaction
    resync(project)
    shutil.copytree(project.root, kg_env / "orphan")  # a project whose harness is gone: its directory still counts
    (kg_env / "sessions").mkdir(exist_ok=True)
    (kg_env / "_shared").mkdir()
    shared = Memo(kg_env / "_shared" / "memo.sqlite")
    shared.run("probe", "1", {}, lambda: {"text": f"SECRET_KEY = '{LITERAL}'"})
    shared.close()
    assert [name for name, _ in project_dirs(kg_env)] == ["_shared", "demo", "orphan"]

    code, out, err = kg("--all", "--json", capsys=capsys)
    assert code == 0, err
    assert not leaked(out + err)
    body = json.loads(out)
    assert [p["project"] for p in body["projects"]] == ["_shared", "demo", "orphan"]
    counts = {p["project"]: p["counts"] for p in body["projects"]}
    assert counts["demo"] == counts["orphan"] and counts["_shared"]["memo"] == 1
    assert body["totals"][OLD] == 2 * counts["demo"][OLD]
    assert kg("--all", "--project", "demo", capsys=capsys)[0] == 2


# Helpers


def test_run_summary_says_whether_a_run_was_full(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a", rev="1")])
    full = Path(sync_project(project)[0].log)
    fake.set(items=[doc("a", rev="2")], listing=False)
    partial = Path(sync_project(project)[0].log)
    assert run_summary(full).full and not run_summary(partial).full
    assert run_summary(full).source == "docs" and len(run_summary(full).refs) >= 1
    with gzip.open(full, "rt") as handle:
        assert json.loads(handle.readline())["run_id"] == run_summary(full).run_id


@pytest.mark.parametrize("path, text", [(v[0], v[1]) for v in REDACTED.values()], ids=REDACTED.keys())
def test_scrub_reads_every_text_the_patterns_redact_past_its_quick_check(path, text):
    assert redact_file(text, path)[1]
    for variant in (text, text.upper(), text.lower()):
        assert not redact_file(variant, path)[1] or might_hold_secret(variant), variant


def test_scrub_quick_check_passes_the_fixture_files_and_skips_plain_code():
    for path, text in (("docker-compose.yml", COMPOSE), ("sa.json", SERVICE_ACCOUNT), ("settings.py", SETTINGS)):
        assert redact_file(text, path)[1] and might_hold_secret(text), path
    assert not might_hold_secret('def search(query):\n    return rank({"key": query, "uri": "https://a.b/c"})\n')


def test_secrets_in_json_reads_keys_and_the_text_in_strings():
    assert secrets_in_json(json.dumps({"password": LITERAL})) == {"password": 1}
    assert secrets_in_json(json.dumps({"text": f"line\nexport JWT_SECRET={LITERAL}\n"})) == {"secret": 1}
    assert secrets_in_json(json.dumps({"private_key": PEM.replace("\\n", "\n")})) == {"pem": 1}
    assert secrets_in_json(json.dumps({"password": "${DB_PASSWORD}", "name": "token"})) == {}


def test_scrub_runs_without_the_hub():
    code = "import sys, evo_agents.kg.scrub, evo_agents.kg.cli_scrub\n"
    code += "print(sorted(m for m in sys.modules if m.startswith(('evo_agents.hub', 'fastapi', 'sqlalchemy'))))"
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env, cwd=root)
    assert out.stdout.strip() == "[]"
    assert scrub_module.__doc__ and "never" in scrub_module.__doc__
