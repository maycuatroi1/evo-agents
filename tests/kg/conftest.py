import json
from pathlib import Path

import pytest

from evo_agents.kg.project import load_project_at
from tests.kg.hub_fake import KgHubs


class FakeSource:
    def __init__(self, path: Path):
        self.path = path
        self.spec = {"items": []}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.spec))

    def set(self, **spec):
        self.spec = {"items": [], **spec}
        self.save()


@pytest.fixture(autouse=True)
def _not_signed_in(tmp_path, monkeypatch):
    """No test of the knowledge graph signs in to the hub of this machine: ``kg sync`` talks to the hub it is signed
    in to, so a test that wants one signs in to a fake with ``hub_for``."""
    monkeypatch.setenv("EVO_HUB_HOME", str(tmp_path / "no-hub-login"))


@pytest.fixture
def hub_for(tmp_path, monkeypatch):
    """``hub_for(*names)``: this machine signed in to an in-memory hub of projects ``names`` (``KgHubs``)."""
    from evo_agents.hub import client, kg_hub_sync, kg_push, kg_remote

    assert kg_remote.Hub is client.Hub  # imported before the patch below, so it keeps the real Hub after the test

    opened: list[KgHubs] = []

    def sign_in(*names: str) -> KgHubs:
        hub = KgHubs(tmp_path / "hub", list(names))
        opened.append(hub)
        credentials = client.Credentials(hub.url, "alice", "evo_test")
        monkeypatch.setattr(client, "load_credentials", lambda: credentials)
        monkeypatch.setattr(client, "Hub", lambda url, token, **kwargs: hub)
        monkeypatch.setattr(kg_hub_sync, "Hub", lambda url, token, **kwargs: hub)
        monkeypatch.setattr(kg_push, "put_presigned", hub.put)
        return hub

    yield sign_in
    for hub in opened:
        hub.close()


@pytest.fixture
def kg_env(tmp_path, monkeypatch):
    home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    return home


@pytest.fixture
def fake_project(tmp_path, kg_env):
    """A harness with one fake source; returns (project, fake)."""
    root = tmp_path / "proj-harness"
    root.mkdir()
    fake = FakeSource(tmp_path / "fake-source.json")
    (root / "harness.yaml").write_text("name: proj\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (root / "knowledge.yaml").write_text(
        f"""
version: 1
project: proj
policy:
  levels: [public, internal, customer]
  sinks:
    - id: test-session
      kind: agent-session
      clearance: {{level: internal}}
sources:
  - id: docs
    connector: "python:tests.kg.fakes:run"
    config: {{file: '{fake.path}'}}
    label: {{level: internal, integrity: U}}
    deletion: {{max_removal_ratio: 0.3, min_scope: 0}}
"""
    )
    return load_project_at(root, kg_env), fake


def doc(key, text=None, rev="1", rev_time="2026-10-01T00:00:00Z"):
    return {"key": key, "text": text or f"# {key}\n\nbody of {key}\n", "rev": rev, "rev_time": rev_time}
