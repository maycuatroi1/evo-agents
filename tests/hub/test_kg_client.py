"""The client side of knowledge graphs on the hub, without Postgres: the knowledge config a push sends and the checks
the hub runs on it, run logs read and labelled as the hub reads them, how ``kg serve`` and ``kg query`` choose their
backend, the push's handling of a hub that refuses, and ``kg sync --push`` on a machine that is not signed in.

Machines are tmp directories with the fake connector of ``tests.kg.fakes``; HOME and EVO_KG_HOME point into tmp."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.kg_cli import RemoteSession, open_session
from evo_agents.hub.kg_ingest import (
    LogProblem,
    config_problems,
    knowledge_config,
    log_header,
    read_run_log,
    refusal,
    run_id_of,
)
from evo_agents.hub.kg_push import Pusher
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import sync_project
from tests.hub import pg

LEVELS = ["public", "internal", "customer", "secret"]
KNOWLEDGE = """\
version: 1
project: alpha
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {{id: hub, kind: hub, clearance: {{level: internal}}}}
sources:
  - id: docs
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{docs}"}}
    label: {{level: internal}}
    refresh: 1h
  - id: vault
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{vault}"}}
    label: {{level: secret}}
"""


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg"))
    harness = tmp_path / "alpha-harness"
    harness.mkdir()
    files = {name: tmp_path / f"{name}.json" for name in ("docs", "vault")}
    (harness / "harness.yaml").write_text("name: alpha\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (harness / "knowledge.yaml").write_text(KNOWLEDGE.format(**files))

    def sync(source: str, *items: dict) -> Path:
        files[source].write_text(json.dumps({"items": list(items)}))
        (result,) = sync_project(load_project_at(harness, tmp_path / "kg"), [source])
        return Path(result.log)

    return load_project_at(harness, tmp_path / "kg"), sync, home


def item(key: str, **extra) -> dict:
    return {"key": key, "text": f"# {key}\n\nabout {key}\n", "rev": "1", **extra}


def cleared_by(level: int):
    return lambda label: label.below(Label(level, 0, "U", frozenset({"alpha"})))


# The config


def test_the_config_keeps_what_the_build_reads_and_nothing_else(machine):
    project, _, _ = machine
    config = knowledge_config(project)
    assert config == {
        "knowledge": {
            "project": "alpha",
            "policy": project.knowledge["policy"],
            "identifiers": [],
            "sources": [
                {"id": "docs", "connector": "python:tests.kg.fakes:run", "label": {"level": "internal"}},
                {"id": "vault", "connector": "python:tests.kg.fakes:run", "label": {"level": "secret"}},
            ],
        },
        "ontology": None,
    }
    assert config_problems("alpha", LEVELS, ["any"], config) == []
    assert config_problems("beta", LEVELS, ["any"], config) == ["knowledge.yaml names project 'alpha', not beta"]
    assert "differ from the registered" in config_problems("alpha", LEVELS[:2], ["any"], config)[0]
    twice = json.loads(json.dumps(config))
    twice["knowledge"]["sources"][1]["id"] = "docs"
    assert config_problems("alpha", LEVELS, ["any"], twice) == ["a source id is declared twice"]
    ontology = json.loads(json.dumps(config))
    ontology["ontology"] = {"node_kinds": {"Widget": {}}}
    assert config_problems("alpha", LEVELS, ["any"], ontology) == [
        "knowledge.ontology and the ontology must be given together"
    ]


# Run logs


def test_a_run_log_is_read_whole_with_its_blobs_and_labels(machine, tmp_path):
    project, sync, _ = machine
    log = sync("docs", item("guide"), item("pay", label={"level": "customer"}))
    run_id = run_id_of(log)
    assert run_id and log_header(log)["source"] == "docs"
    policy = Policy("alpha", project.knowledge)

    cleared = read_run_log(log, "alpha", run_id, policy, cleared_by(2))
    assert cleared.items == 2 and cleared.refused_count == 0 and refusal(cleared, policy, "hub") is None
    assert len(cleared.blobs) == 2 and all(len(b) == 64 for b in cleared.blobs)  # a body each, fragments alike

    refused = read_run_log(log, "alpha", run_id, policy, cleared_by(1))
    assert refused.refused_count == 1 and refused.refused[0][0] == "docs:doc:pay"
    assert "1 item(s) of run" in refusal(refused, policy, "hub") and "docs:doc:pay (customer)" in refusal(
        refused, policy, "hub"
    )

    damaged = tmp_path / "damaged.jsonl.gz"
    damaged.write_bytes(log.read_bytes()[:-30])
    with pytest.raises(LogProblem, match="cut short"):
        read_run_log(damaged, "alpha", run_id, policy, cleared_by(3))
    lines = gzip.decompress(log.read_bytes()).splitlines()
    at = next(i for i, line in enumerate(lines) if json.loads(line)["type"] == "item")
    record = json.loads(lines[at])
    record["body"]["blob"] = "md5:abc"
    bad_ref = tmp_path / "bad.jsonl.gz"
    bad_ref.write_bytes(gzip.compress(b"\n".join([*lines[:at], json.dumps(record).encode(), *lines[at + 1 :]])))
    with pytest.raises(LogProblem, match="refers to a blob that is not sha256"):
        read_run_log(bad_ref, "alpha", run_id, policy, cleared_by(3))
    with pytest.raises(LogProblem, match="does not start with the header"):
        headless = tmp_path / "headless.jsonl.gz"
        headless.write_bytes(gzip.compress(b"\n".join(lines[1:])))
        read_run_log(headless, "alpha", run_id, policy, cleared_by(3))
    assert run_id_of(tmp_path / "notes.jsonl.gz") is None and run_id_of(log.with_suffix("")) is None


# Pushing


class RefusingHub:
    url = "https://hub.test"

    def __init__(self, status: int):
        self.status = status
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        raise HubError(f"refused with {self.status}", self.status)


def test_a_push_stops_at_the_project_or_at_the_credential_the_hub_refuses(machine):
    project, sync, _ = machine
    sync("docs", item("guide"))
    report = Pusher(RefusingHub(404), project).push()
    assert report.errors == ["refused with 404"] and report.pushed == []
    hub = RefusingHub(401)
    with pytest.raises(HubError):
        Pusher(hub, project).push()
    assert hub.calls == [("PUT", "/v1/kg/alpha/config")]


def test_kg_sync_push_without_signing_in_fails_after_syncing(machine, capsys):
    project, sync, _ = machine
    sync("docs", item("guide"))
    code = main(["kg", "sync", "--project", str(project.harness.root), "--source", "docs", "--push"])
    out = capsys.readouterr().out
    assert code == 1 and "FAIL push: not signed in to a hub" in out and "OK   docs" in out


# Choosing the backend


def sign_in(home: Path, url: str) -> None:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text("evh_" + "x" * 43 + "\n")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": "alice"}))


def test_auto_answers_locally_unless_signed_in_to_a_hub_with_the_graph(machine):
    project, _, home = machine
    local = open_session("auto", str(project.harness.root), "hub")
    assert isinstance(local, Session) and local.project.name == "alpha"
    refused = open_session("hub", str(project.harness.root), "hub")
    assert (
        isinstance(refused, Session) and "not signed in to a hub" in refused.call("kg_status", {})["content"][0]["text"]
    )

    dead = f"http://127.0.0.1:{pg.free_port()}"
    sign_in(home, dead)
    assert isinstance(open_session("auto", str(project.harness.root), "hub"), Session)  # the hub does not answer
    remote = open_session("hub", str(project.harness.root), "hub")
    assert isinstance(remote, RemoteSession) and remote.project == "alpha"
    answer = remote.call("kg_search", {"query": "x"})
    assert answer["isError"] and dead in answer["content"][0]["text"]
    assert remote.call("kg_reveal", {})["content"][0]["text"] == "error: unknown tool 'kg_reveal'"
    named = open_session("hub", "beta", "hub")  # a project only the hub knows, by name
    assert isinstance(named, RemoteSession) and named.project == "beta"
    assert isinstance(Hub(dead).url, str)


# The api's cache of built graphs


class FakeStore:
    """``BlobStore.fetch`` over a dict of blobs; ``down`` makes it raise as R2 going away."""

    def __init__(self, blobs: dict[str, bytes]):
        self.blobs = blobs
        self.down = False
        self.fetched = []

    def fetch(self, key, target, limit):
        from evo_agents.hub.blobs import BlobStoreUnavailable

        if self.down:
            raise BlobStoreUnavailable("the blob store did not complete GetObject (EndpointConnectionError)")
        self.fetched.append(key)
        data = self.blobs.get(key.rsplit("/", 1)[-1])
        if data is None:
            return None
        Path(target).write_bytes(data[: limit + 1])
        import hashlib

        return hashlib.sha256(data[: limit + 1]).hexdigest(), len(data[: limit + 1])


def test_the_graph_cache_keeps_two_checks_every_fetch_and_never_deletes_a_graph_in_use(tmp_path):
    pytest.importorskip("boto3")
    import hashlib
    import os
    import time as clock
    from datetime import datetime, timezone

    from evo_agents.hub.kg_graph import ArtifactMismatch, BuiltGraph, GraphCache, GraphUnavailable

    payloads = {n: f"graph {n}".encode() * 100 for n in range(1, 5)}
    blobs = {hashlib.sha256(data).hexdigest(): data for data in payloads.values()}
    store = FakeStore(blobs)
    now = datetime.now(timezone.utc)
    graphs = {
        n: BuiltGraph(n, hashlib.sha256(data).hexdigest(), len(data), "sha256:" + "0" * 64, 1, 0, now)
        for n, data in payloads.items()
    }
    cache = GraphCache(tmp_path / "graphs")

    def newest_first(n: int) -> list:
        return [graphs[i] for i in range(n, 0, -1)]

    held, first = cache.acquire(store, "alpha", newest_first(1))
    assert first.build_id == 1 and held.read_bytes() == payloads[1]
    for n in (2, 3):
        path, _ = cache.acquire(store, "alpha", newest_first(n))
        cache.release(path)
        clock.sleep(0.01)
    # build 1 is being read: it stays although it is not among the two installed last
    assert sorted(p.name for p in held.parent.iterdir()) == sorted(f"{graphs[n].sha256}.sqlite" for n in (1, 2, 3))
    cache.release(held)
    assert cache.evict("alpha") == [held]
    path, again = cache.acquire(store, "alpha", newest_first(3))
    assert again.build_id == 3 and len(store.fetched) == 3  # cached: not fetched again
    cache.release(path)

    store.blobs[graphs[4].sha256] = b"x" * graphs[4].size  # the store returns other bytes
    with pytest.raises(ArtifactMismatch):
        cache.acquire(store, "alpha", newest_first(4))
    assert not (cache.path("alpha", graphs[4].sha256)).exists() and not [
        p for p in os.listdir(held.parent) if p.endswith(".part")
    ]
    store.down = True  # R2 down: the newest cached graph answers
    path, fallback = cache.acquire(store, "alpha", newest_first(4))
    assert fallback.build_id == 3
    cache.release(path)
    with pytest.raises(GraphUnavailable):
        cache.acquire(store, "beta", newest_first(4))
