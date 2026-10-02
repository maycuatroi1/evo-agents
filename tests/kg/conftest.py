import json
from pathlib import Path

import pytest

from evo_agents.kg.project import load_project_at


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
    config: {{file: "{fake.path}"}}
    label: {{level: internal, integrity: U}}
    deletion: {{max_removal_ratio: 0.3, min_scope: 0}}
"""
    )
    return load_project_at(root, kg_env), fake


def doc(key, text=None, rev="1", rev_time="2026-10-01T00:00:00Z"):
    return {"key": key, "text": text or f"# {key}\n\nbody of {key}\n", "rev": rev, "rev_time": rev_time}
