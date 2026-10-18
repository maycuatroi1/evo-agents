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
import random
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
from evo_agents.kg.corpus import BASE_EPOCH, Corpus
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
    rewrite_log,
    run_summary,
    scan_project,
    secrets_in_json,
)
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import accept_removals, sync_project, sync_source
from tests.kg.conftest import doc
from tests.kg.projects import make_project
from tests.kg.test_lineage import commit, git, git_project
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
        patched.setattr(git_connector, "read_input", lambda ctx: "sha256:a connector that does not redact")
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
    logs = len(scan_project(project.name, project.root).logs)
    assert logs and f"  run logs: {logs} of old runs name a blob --purge deletes; they go with it" in out

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


LEAKY = f"# A\n\n```\nDB_PASSWORD: {LITERAL}\n```\n"
VAULT = "# A\n\nThe password is in the vault.\n"


def records_of(path: Path) -> list[dict]:
    """Every line of the run log at ``path``, read strictly: a file cut short or not gzip raises."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def blob_files(root: Path) -> set[str]:
    return {path.parent.name + path.name for path in (root / "blobs" / "sha256").glob("*/*")}


def states(project) -> tuple[str, str]:
    """The digest of the merged state, and of the state the run logs replay to."""
    corpus = Corpus(project.name, project.home)
    try:
        return corpus.state_digest(), corpus.state_digest(corpus.rebuild_state())
    finally:
        corpus.close()


def scrub_line(project) -> dict:
    lines = [json.loads(line) for line in (project.root / "audit.jsonl").read_text().splitlines()]
    (line,) = [line for line in lines if line["event"] == "scrub"]
    return line


def assert_compacted(project, kept: set[str], state: str, content: str) -> None:
    """After a purge that rewrote run logs in use: the kept blobs are gone and no file holds a value, the merged
    state is the same and the logs left replay to it, and a cold verified build gives the same graph."""
    assert kept and not kept & blob_files(project.root)
    assert files_holding(project.root) == []
    assert states(project) == (state, state)
    verified = build_project(project, verify=True, cold=True)
    assert verified.ok and verified.verify["match"] and verified.content_hash == content
    assert scan_project(project.name, project.root).clean
    assert not list((project.root / "log").glob(".*"))  # no temporary file left


def test_scrub_purge_compacts_a_run_log_whose_item_record_from_before_redaction_names_a_kept_blob(fake_project, capsys):
    project, fake = fake_project
    fake.set(items=[doc("a", LEAKY, rev="1"), doc("b", rev="1")])
    first = Path(sync_project(project)[0].log)
    fake.set(items=[doc("a", VAULT, rev="2")], listing=False)  # b not listed: the first run still holds b's record
    sync_project(project)
    content = build_project(project).content_hash
    state, replayed = states(project)
    assert state == replayed

    found = scan_project(project.name, project.root)
    assert {(f.state, tuple(f.items), tuple(f.secrets.items())) for f in found.blobs} == {
        (KEPT, ("docs:doc:a",), (("password", 1),))
    }
    assert found.logs == [] and found.rewrite == [first.name] and found.counts()["rewrite"] == 1
    before = records_of(first)
    code, out, err = kg("--project", project.name, capsys=capsys)
    assert code == 0, err
    assert f"kept {len(found.blobs)}: only records the corpus no longer holds refer to them" in out
    assert "--purge rewrites those 1 run log(s) without such records, then deletes them" in out

    code, out, err = kg("--project", project.name, "--purge", "--yes", capsys=capsys)
    assert code == 0, err
    assert not leaked(out + err)
    assert f"About to delete 0 old blob(s) and {len(found.blobs)} kept blob(s)" in out
    assert "after rewriting 1 run log(s) in use" in out
    assert f"{project.name}: deleted {len(found.blobs)} blob(s)" in out
    assert "rewrote 1 run log(s) in use without the 1 record(s) the corpus no longer holds" in out

    after = records_of(first)  # the same name, still a complete gzip file
    assert after == [record for record in before if record.get("id") != "docs:doc:a"]
    assert after[0]["type"] == "run" and len(after) == len(before) - 1
    assert [record["id"] for record in after if record["type"] == "item"] == ["docs:doc:b"]
    assert_compacted(project, {f.sha256 for f in found.blobs}, state, content)
    line = scrub_line(project)
    assert (line["rewritten"], line["dropped"], line["blobs"], line["kept"], line["logs"]) == (
        1,
        1,
        len(found.blobs),
        len(found.blobs),
        0,
    )
    assert line["secrets"] == {"password": len(found.blobs)} and not leaked(json.dumps(line))

    fake.set(items=[doc("a", VAULT, rev="3"), doc("b", rev="2")])  # syncs go on from the rewritten logs
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok and states(project)[0] == states(project)[1]


def test_scrub_purge_compacts_a_run_log_held_in_use_by_its_winning_tombstone(fake_project):
    """A full run that removed an item stays in use while its tombstone wins; its records of the items it listed
    before they were redacted then keep their blobs. The purge keeps the tombstone and drops those records."""
    project, fake = fake_project
    others = [doc(key, rev="1") for key in "bcd"]
    fake.set(items=[doc("a", LEAKY, rev="1"), *others, doc("e", rev="1")])
    first = Path(sync_project(project)[0].log)
    fake.set(items=[doc("a", LEAKY, rev="1"), *others])  # e is gone: the complete listing removes it
    removing = sync_project(project)[0]
    assert removing.removals == 1
    second = Path(removing.log)
    fake.set(items=[doc("a", VAULT, rev="2"), *others])
    sync_project(project)
    content = build_project(project).content_hash
    state, _ = states(project)

    found = scan_project(project.name, project.root)
    kept = {f.sha256 for f in found.found(KEPT)}
    assert kept and found.found(OLD) == [] and found.found(LIVE) == []
    assert found.rewrite == [second.name]  # in use for e's tombstone
    assert found.logs == [first.name]  # no record of the first run wins any more
    with holding_sources(project.root):
        deleted = purge(found)
    assert (deleted["blobs"], deleted["logs"], deleted["rewritten"], deleted["dropped"]) == (len(kept), 1, 1, 4)

    assert not first.exists()
    entries = [record for record in records_of(second) if record["type"] in ("item", "tombstone")]
    assert entries == [
        {"type": "tombstone", "id": "docs:doc:e", "rev": "1", "rev_time": "2026-10-01T00:00:00Z", "derived": "listing"}
    ]
    assert_compacted(project, kept, state, content)


def test_scrub_purge_drops_the_records_a_change_of_ref_superseded(tmp_path, kg_env, monkeypatch):
    """A source moved to a ref whose commits are older: its next run starts a new epoch and supersedes what the old
    ref gave, newer commit times and all. A removal the guard held keeps the old run in use, so its record of a file
    read before redaction keeps that blob. The purge counts the superseded record as lost and drops it; the logs left,
    whose run lines still name their epochs, replay in any order to the merged state."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "2026-09-01T09:00:00Z", {"docs/guide.md": "# Guide\n", "docker-compose.yml": "services: {}\n"})
    git(repo, "branch", "deploy")
    commit(repo, "2026-10-01T09:00:00Z", {"docker-compose.yml": COMPOSE, "docs/main-only.md": "# Main only\n"})
    guard = "    deletion: {max_removal_ratio: 0.1, min_scope: 0}\n"
    with monkeypatch.context() as patched:
        patched.setattr(git_connector, "redact_file", lambda text, path: (text, {}))
        patched.setattr(git_connector, "read_input", lambda ctx: "sha256:a connector that does not redact")
        main_run = sync_source(git_project(tmp_path, kg_env, repo, "main", guard), "app")
    assert main_run.ok and files_holding(kg_env / "proj")  # the control: the value is on disk
    project = git_project(tmp_path, kg_env, repo, "deploy", guard)
    switched = sync_source(project, "app")
    assert switched.ok and switched.held and switched.removals == 0  # docs/main-only.md stays, from the main run

    corpus = project.corpus()
    compose = corpus.get("app:file:docker-compose.yml")
    assert compose.run_id == switched.run_id and compose.record["rev_time"] < "2026-10-01"  # older, and it won
    assert corpus.get("app:file:docs/main-only.md").run_id == main_run.run_id
    content = build_project(project).content_hash
    state, replayed = states(project)
    assert state == replayed

    first = Path(main_run.log)
    found = scan_project(project.name, project.root)
    kept = {f.sha256 for f in found.found(KEPT)}
    assert {(f.state, tuple(f.items)) for f in found.blobs} == {(KEPT, ("app:file:docker-compose.yml",))}
    assert found.rewrite == [first.name] and found.logs == []
    with holding_sources(project.root):
        deleted = purge(found)
    assert (deleted["blobs"], deleted["logs"], deleted["rewritten"], deleted["dropped"]) == (1, 0, 1, 2)

    entries = [record for record in records_of(first) if record["type"] in ("item", "tombstone")]
    assert [record["id"] for record in entries] == ["app:file:docs/main-only.md"]
    assert records_of(first)[0]["epoch"] == BASE_EPOCH
    assert_compacted(project, kept, state, content)
    corpus = Corpus(project.name, project.home)
    try:
        rng = random.Random(5)
        for _ in range(4):
            order = corpus.log_files() * 2
            rng.shuffle(order)
            assert corpus.state_digest(corpus.rebuild_state(order)) == state
    finally:
        corpus.close()

    assert accept_removals(project, "app") == 1  # the held removal goes on from the rewritten log
    assert states(project)[0] == states(project)[1]
    assert project.corpus().get("app:file:docs/main-only.md").deleted


def test_scrub_purge_reads_a_corpus_no_sync_opened_since_epochs(fake_project):
    """A kg home an older version wrote: run lines without an epoch, and no k_epoch column, which a scan, reading the
    corpus without writing it, does not add. Its records have the empty epoch, so the purge still finds the records
    its runs won."""
    project, fake = fake_project
    fake.set(items=[doc("a", LEAKY, rev="1"), doc("b", rev="1")])
    first = Path(sync_project(project)[0].log)
    fake.set(items=[doc("a", VAULT, rev="2")], listing=False)
    sync_project(project)
    for path in (project.root / "log").glob("*.jsonl.gz"):
        records = [{k: v for k, v in r.items() if k not in ("epoch", "lineage")} for r in records_of(path)]
        path.write_bytes(gzip.compress("".join(json.dumps(r, sort_keys=True) + "\n" for r in records).encode()))
    db = sqlite3.connect(project.root / "corpus.sqlite")
    with db:
        db.execute("ALTER TABLE items DROP COLUMN k_epoch")
        db.execute("DROP TABLE lineages")
    db.close()
    content = build_project(project).content_hash
    state, replayed = states(project)  # opening the corpus adds the column back, every record with the empty epoch
    assert state == replayed
    db = sqlite3.connect(project.root / "corpus.sqlite")
    with db:
        db.execute("ALTER TABLE items DROP COLUMN k_epoch")
    db.close()

    found = scan_project(project.name, project.root)
    assert found.rewrite == [first.name] and len(found.found(KEPT)) == 1
    with holding_sources(project.root):
        deleted = purge(found)
    assert (deleted["rewritten"], deleted["dropped"], found.warnings) == (1, 1, [])
    db = sqlite3.connect(f"{(project.root / 'corpus.sqlite').as_uri()}?mode=ro", uri=True)
    assert "k_epoch" not in {row[1] for row in db.execute("PRAGMA table_info(items)")}
    db.close()
    assert_compacted(project, {f.sha256 for f in found.found(KEPT)}, state, content)


def test_scrub_keeps_a_kept_blob_a_log_still_being_written_names(fake_project, capsys):
    project, fake = fake_project
    fake.set(items=[doc("a", LEAKY, rev="1"), doc("b", rev="1")])
    first = Path(sync_project(project)[0].log)
    fake.set(items=[doc("a", VAULT, rev="2")], listing=False)
    sync_project(project)
    crashed = project.root / "log" / "01890000-0000-7000-8000-000000000000.jsonl"  # as a sync that crashed leaves it
    crashed.write_text("".join(json.dumps(record) + "\n" for record in records_of(first)), encoding="utf-8")

    found = scan_project(project.name, project.root)
    kept = {f.sha256 for f in found.found(KEPT)}
    assert kept and found.pinned == kept and found.doomed() == [] and found.rewrite == []
    code, out, err = kg("--project", project.name, capsys=capsys)
    assert f"{len(kept)} of them stay, named by a run log a sync left unfinished" in out
    with holding_sources(project.root):
        assert purge(found)["blobs"] == 0
    assert kept <= blob_files(project.root)


def test_rewrite_log_leaves_a_log_lacking_a_record_the_corpus_holds(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a", LEAKY, rev="1"), doc("b", rev="1")])
    first = Path(sync_project(project)[0].log)
    fake.set(items=[doc("a", VAULT, rev="2")], listing=False)
    sync_project(project)
    winners, _ = scrub_module._winners(project.root)
    run_id = records_of(first)[0]["run_id"]
    before = first.read_bytes()

    assert rewrite_log(first, winners, {run_id: {"docs:doc:b", "docs:doc:gone"}}) is None
    assert first.read_bytes() == before and not list(first.parent.glob(".*"))
    assert rewrite_log(first, winners, {run_id: {"docs:doc:b"}}) == 1


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
    assert run_summary(full).paths == {}  # these items name no file
    fake.set(items=[{**doc("a", rev="3"), "path": "docs/a.md"}])
    named = run_summary(Path(sync_project(project)[0].log))
    assert named.paths and set(named.paths) == named.refs and set().union(*named.paths.values()) == {"docs/a.md"}
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
