"""Secrets in text (``evo_agents.kg.scrub``, ``evo_agents.sqlite_legacy.graph_text_rows``), what the hub's scrub reads with, and
``evo-agents kg scrub`` and ``kg prune``, which have the hub scrub and prune a project: a machine keeps no corpus, memo
or graph of its own to clean. Nothing these read answers a value."""

import gzip
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub.cli import kg as kg_cli
from evo_agents.kg import scrub as scrub_module
from evo_agents.kg.redact import redact_file
from evo_agents.kg.scrub import might_hold_secret, run_summary, secrets_in_json
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc
from tests.kg.test_redact import COMPOSE, LITERAL, PEM, REDACTED, SERVICE_ACCOUNT, SETTINGS


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


def old_graph_file(
    path: Path, texts: list[tuple[str, str]], props: list[tuple[str, str]], *, edges: bool = True
) -> Path:
    """A graph file as builds wrote before the hub kept graphs in its tables: ``texts`` in its full-text index,
    ``props`` in its nodes, and an empty edges table unless ``edges`` is false. The hub's tests build theirs here,
    where the raw SQL guard of the hub does not look."""
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE node_fts (name TEXT, body TEXT)")
    db.execute("CREATE TABLE nodes (name TEXT, props TEXT)")
    if edges:
        db.execute("CREATE TABLE edges (props TEXT)")
    db.executemany("INSERT INTO node_fts VALUES (?, ?)", texts)
    db.executemany("INSERT INTO nodes VALUES (?, ?)", props)
    db.commit()
    db.close()
    return path


def test_graph_secrets_counts_the_rows_of_a_graph_file_holding_one(tmp_path):
    pytest.importorskip("fastapi", reason="the hub's scrub needs the hub-server extra, evo-ak[hub-server]")
    from evo_agents.hub.server.kg_scrub import graph_secrets

    path = old_graph_file(
        tmp_path / "old-graph.db",
        [("settings", f"export JWT_SECRET={LITERAL}\n"), ("readme", "Install.")],
        [("db", json.dumps({"password": LITERAL})), ("a", "{}")],
        edges=False,  # a store of another schema
    )
    assert graph_secrets(path) == (2, {"password": 1, "secret": 1})


def asked(monkeypatch, command: str) -> list:
    """The arguments ``kg <command>`` hands the hub's command, which is not run."""
    seen = []
    monkeypatch.setattr(kg_cli, f"cmd_{command}", lambda args: seen.append(args) or 0)
    return seen


def test_kg_scrub_has_the_hub_scrub_the_project(monkeypatch):
    seen = asked(monkeypatch, "scrub")
    assert main(["kg", "scrub", "--project", "northwind"]) == 0
    assert main(["kg", "scrub", "--all", "--purge", "--yes", "--json"]) == 0
    assert [(a.project, a.purge, a.yes, a.json) for a in seen] == [
        ("northwind", False, False, False),
        (None, True, True, True),
    ]
    assert main(["kg", "scrub", "--all", "--project", "northwind"]) == 2 and len(seen) == 2


def test_kg_prune_counts_on_the_hub_unless_told_to_delete(monkeypatch):
    seen = asked(monkeypatch, "prune")
    assert main(["kg", "prune", "--project", "northwind"]) == 0
    assert main(["kg", "prune", "--all", "--keep", "2", "--delete"]) == 0
    assert [(a.project, a.keep, a.dry_run, a.graph_files) for a in seen] == [
        ("northwind", None, True, False),
        (None, 2, False, False),
    ]


def test_scrub_runs_without_the_hub():
    code = "import sys, evo_agents.kg.scrub, evo_agents.sqlite_legacy\n"
    code += "print(sorted(m for m in sys.modules if m.startswith(('evo_agents.hub', 'fastapi', 'sqlalchemy'))))"
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env, cwd=root)
    assert out.stdout.strip() == "[]"
    assert scrub_module.__doc__ and "never" in scrub_module.__doc__
