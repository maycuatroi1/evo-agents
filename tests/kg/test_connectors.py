import os
import subprocess
from pathlib import Path

import pytest

from evo_agents.harness import Harness
from evo_agents.kg.protocol.conformance import run_conformance
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorRun

GOLDEN = Path(__file__).parent / "golden"
DATE = "2026-10-01T09:00:00+07:00"


def sh(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": DATE,
        "GIT_COMMITTER_DATE": DATE,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def make_repo(root: Path) -> Path:
    root.mkdir()
    sh(root, "init", "-q", "-b", "main")
    sh(root, "remote", "add", "origin", "git@github.com:acme/app.git")
    (root / "docs").mkdir()
    (root / "docs/guide.md").write_text("# Guide\n\nIntro.\n\n## Setup\n\nRun `app.server::serve`.\n")
    (root / "app").mkdir()
    (root / "app/server.py").write_text(
        "import os\n\n\ndef serve(port=8000):\n    return port\n\n\nclass Handler:\n    def get(self):\n"
        "        return serve()\n"
    )
    (root / "config.yaml").write_text("name: app\n")
    (root / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    (root / "node_modules").mkdir()
    (root / "node_modules/x.js").write_text("ignored")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "init")
    return root


def run_git(repo: Path, source_id="app"):
    source = {"id": source_id, "connector": "git", "path": str(repo)}
    return list(ConnectorRun(ConnectorContext("p", source)))


def items_by_path(messages):
    return {m["props"]["path"]: m for m in messages if m["type"] == "item"}


def test_git_connector_reads_committed_files(tmp_path):
    repo = make_repo(tmp_path / "app")
    (repo / "docs/guide.md").write_text("uncommitted edit\n")  # the working tree must not leak in
    msgs = run_git(repo)
    items = items_by_path(msgs)
    assert sorted(items) == ["app/server.py", "config.yaml", "docs/guide.md"]
    guide = items["docs/guide.md"]
    assert [f["anchor"] for f in guide["fragments"]] == ["guide", "setup"]
    assert "uncommitted" not in guide["body"]["text"]
    py = items["app/server.py"]
    assert [f["anchor"] for f in py["fragments"]] == ["_module", "serve", "Handler"]
    assert py["body"]["language"] == "python"
    assert msgs[-2] == {"type": "listing", "scope": "app:file:", "complete": True, "count": 3}


def test_git_revision_changes_only_with_content(tmp_path):
    repo = make_repo(tmp_path / "app")
    before = items_by_path(run_git(repo))
    (repo / "config.yaml").write_text("name: app2\n")
    sh(repo, "commit", "-qam", "edit")
    after = items_by_path(run_git(repo))
    changed = [p for p in before if before[p]["hash"] != after[p]["hash"]]
    assert changed == ["config.yaml"]
    assert before["docs/guide.md"]["rev"] == after["docs/guide.md"]["rev"]


def test_git_connector_golden(tmp_path):
    repo = make_repo(tmp_path / "app")
    report = run_conformance({"id": "app", "connector": "git", "path": str(repo)}, golden=GOLDEN / "git.jsonl")
    assert report.ok, report.to_json()


def test_missing_repo_is_a_config_error_not_an_empty_listing(tmp_path):
    msgs = run_git(tmp_path / "nowhere")
    kinds = [m["type"] for m in msgs]
    assert "listing" not in kinds
    assert msgs[-1]["status"] == "error"


@pytest.fixture
def harness_dir(tmp_path):
    root = tmp_path / "h"
    (root / "plans/active").mkdir(parents=True)
    (root / "bindings").mkdir()
    (root / "state").mkdir()
    (root / "harness.yaml").write_text("name: h\nrepos: []\n")
    (root / "contracts.yaml").write_text("seams: []\n")
    (root / "plans/active/p.yaml").write_text("id: p\nsteps: []\n")
    (root / "bindings/core.yaml").write_text("bindings: []\n")
    (root / "CLUSTER.md").write_text("# Map\n")
    (root / "state/scan.json").write_text("{}")
    return root


def test_harness_connector_classifies_files(harness_dir):
    source = {"id": "harness", "connector": "harness"}
    msgs = list(ConnectorRun(ConnectorContext("p", source, Harness(harness_dir, {"name": "h"}))))
    kinds = {m["props"]["path"]: m["kind"] for m in msgs if m["type"] == "item"}
    assert kinds == {
        "CLUSTER.md": "markdown",
        "bindings/core.yaml": "binding",
        "contracts.yaml": "contracts",
        "harness.yaml": "manifest",
        "plans/active/p.yaml": "plan",
    }
    report = run_conformance(source, harness=Harness(harness_dir, {"name": "h"}))
    assert report.ok, report.to_json()
