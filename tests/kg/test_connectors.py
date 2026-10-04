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


def run_git(repo: Path, source_id="app", **keys):
    source = {"id": source_id, "connector": "git", "path": str(repo), **keys}
    return list(ConnectorRun(ConnectorContext("p", source)))


def items_by_path(messages):
    return {m["props"]["path"]: m for m in messages if m["type"] == "item"}


def test_git_connector_reads_committed_files(tmp_path):
    repo = make_repo(tmp_path / "app")
    (repo / "docs/guide.md").write_text("uncommitted edit\n")  # the working tree must not leak in
    msgs = run_git(repo)
    items = items_by_path(msgs)
    assert sorted(items) == ["app/server.py", "config.yaml", "docs/guide.md", "logo.png"]
    assert items["logo.png"]["kind"] == "asset" and "body" not in items["logo.png"]
    guide = items["docs/guide.md"]
    assert [f["anchor"] for f in guide["fragments"]] == ["guide", "setup"]
    assert "uncommitted" not in guide["body"]["text"]
    py = items["app/server.py"]
    assert [f["anchor"] for f in py["fragments"]] == ["_module", "serve", "Handler"]
    assert py["body"]["language"] == "python"
    assert msgs[-2] == {"type": "listing", "scope": "app:file:", "complete": True, "count": 4}


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


BASE = ["app/server.py", "config.yaml", "docs/guide.md", "logo.png"]


def add_agent_files(repo: Path) -> Path:
    (repo / ".claude").mkdir()
    (repo / ".claude/CLAUDE.md").write_text("# Rules\n\nRun tests with uv.\n")
    (repo / ".claude/settings.json").write_text("{}\n")
    (repo / ".claude/icon.png").write_bytes(b"\x89PNG\0\0binary")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "agent files")
    return repo


def test_git_default_excludes_still_drop_claude_dir(tmp_path):
    repo = add_agent_files(make_repo(tmp_path / "app"))
    assert sorted(items_by_path(run_git(repo))) == BASE


def test_git_allow_reopens_exactly_the_allowed_path(tmp_path):
    repo = add_agent_files(make_repo(tmp_path / "app"))
    msgs = run_git(repo, allow=[".claude/CLAUDE.md"])
    items = items_by_path(msgs)
    assert sorted(items) == sorted([*BASE, ".claude/CLAUDE.md"])
    claude = items[".claude/CLAUDE.md"]
    assert claude["kind"] == "markdown" and "Run tests with uv." in claude["body"]["text"]
    assert msgs[-2]["count"] == 5


def test_git_source_exclude_wins_over_allow(tmp_path):
    repo = add_agent_files(make_repo(tmp_path / "app"))
    items = items_by_path(run_git(repo, allow=[".claude/*"], exclude=[".claude/CLAUDE.md"]))
    assert sorted(items) == sorted([*BASE, ".claude/icon.png", ".claude/settings.json"])
    assert items[".claude/settings.json"]["kind"] == "config"
    assert items[".claude/icon.png"]["kind"] == "asset"  # allowed but not text-selectable: a path-only asset


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


def test_knowledge_yaml_moves_the_bindings_directory(harness_dir):
    # A harness whose bindings/ holds another tool's format points evo-agents somewhere else.
    (harness_dir / "knowledge.yaml").write_text("version: 1\nproject: p\nbindings: kg/bindings\nsources: []\n")
    (harness_dir / "kg/bindings").mkdir(parents=True)
    (harness_dir / "kg/bindings/links.yaml").write_text("version: 1\nbindings: []\n")
    source = {"id": "harness", "connector": "harness"}
    msgs = list(ConnectorRun(ConnectorContext("p", source, Harness(harness_dir, {"name": "h"}))))
    kinds = {m["props"]["path"]: m["kind"] for m in msgs if m["type"] == "item"}
    assert kinds["kg/bindings/links.yaml"] == "binding"
    assert kinds["bindings/core.yaml"] == "yaml"


def test_harness_repo_is_named_from_the_manifest_in_a_worktree(harness_dir):
    sh(harness_dir, "init", "-q")
    sh(harness_dir, "remote", "add", "origin", "git@github.com:acme/h-harness.git")
    sh(harness_dir, "add", "-A")
    sh(harness_dir, "commit", "-qm", "init")
    manifest = {
        "name": "h",
        "repos": [
            {
                "name": "h-harness",
                "path": "/elsewhere/h-harness",
                "origin": "https://github.com/acme/h-harness.git",
                "default_branch": "master",
            },
        ],
    }
    source = {"id": "harness", "connector": "harness"}
    msgs = list(ConnectorRun(ConnectorContext("p", source, Harness(harness_dir, manifest))))
    item = next(m for m in msgs if m["type"] == "item" and m["props"]["path"] == "CLUSTER.md")
    assert item["props"]["repo"] == "h-harness"
    assert item["uri"] == "https://github.com/acme/h-harness/blob/master/CLUSTER.md"
