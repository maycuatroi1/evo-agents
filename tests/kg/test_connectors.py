import datetime as dt
import gzip
import json
import os
import subprocess
from pathlib import Path

import pytest

from evo_agents.harness import Harness, plan_digest
from evo_agents.kg import retention
from evo_agents.kg.build import build_project
from evo_agents.kg.connectors import _files, _git
from evo_agents.kg.connectors import git as git_connector
from evo_agents.kg.connectors import github as github_api
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import load_project_at
from evo_agents.kg.protocol.conformance import run_conformance
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorRun
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from evo_agents.kg.sync import accept_removals, sync_project
from tests.kg.projects import commit, git, make_project, write

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
    """The file items of a run, by path; the commit items of its history have none."""
    return {m["props"]["path"]: m for m in messages if m["type"] == "item" and m["kind"] != "commit"}


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


@pytest.mark.parametrize(
    ("path", "dropped"),
    [
        ("subjects/DBS401/.claude/settings.json", True),
        ("subjects/DBS401/.claude/settings.local.json", True),
        ("apps/web/.agents/skills/x/SKILL.md", True),
        ("apps/web/.playwright-mcp/page.yml", True),
        ("services/api/venv/lib/site.py", True),
        ("web/yarn.lock", True),
        ("services/api/uv.lock", True),
        ("services/api/poetry.lock", True),
        ("state/run.json", True),
        ("src/state/store.ts", False),
        ("docs/claude.md", False),
    ],
)
def test_default_exclude_drops_agent_folders_venvs_and_locks_at_any_depth(path, dropped):
    assert _files.excluded(path, [], []) is dropped


def test_allow_brings_a_nested_agent_file_back():
    assert _files.excluded("subjects/DBS401/.claude/CLAUDE.md", [], ["*/.claude/CLAUDE.md"]) is False
    assert _files.excluded("subjects/DBS401/.claude/settings.json", [], ["*/.claude/CLAUDE.md"]) is True


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


def clone_behind(tmp_path) -> tuple[Path, Path]:
    """(upstream, clone) where upstream has one commit, docs/new.md, the clone has not fetched."""
    upstream = make_repo(tmp_path / "upstream")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True, capture_output=True)
    (upstream / "docs/new.md").write_text("# New\n")
    sh(upstream, "add", "-A")
    sh(upstream, "commit", "-q", "-m", "new")
    return upstream, clone


def test_git_fetch_reads_the_remote_branch(tmp_path, monkeypatch):
    upstream, clone = clone_behind(tmp_path)
    calls = []
    real = _git.fetch
    monkeypatch.setattr(
        _git,
        "fetch",
        lambda repo, remote, branch, **kw: (calls.append((remote, branch)), real(repo, remote, branch, **kw)),
    )
    msgs = run_git(clone, ref="origin/main", fetch=True)
    assert calls == [("origin", "main")]
    assert "docs/new.md" in items_by_path(msgs)
    assert msgs[-1]["status"] == "ok"
    state = next(m for m in msgs if m["type"] == "state")
    assert state["cursor"]["commit"] == sh(upstream, "rev-parse", "HEAD").strip()


def cursor_of(messages) -> dict:
    """The cursor a run ended with, but for ``input``, the digest of its config and code, and ``at``, the time of the
    read (both tested on their own)."""
    cursor = next(m for m in messages if m["type"] == "state")["cursor"]
    assert cursor["input"].startswith("sha256:")
    dt.datetime.strptime(cursor["at"], "%Y-%m-%dT%H:%M:%SZ")
    return {key: value for key, value in cursor.items() if key not in ("input", "at")}


def test_git_cursor_names_the_commit_read_and_how_far_behind_the_remote_default_branch(tmp_path, monkeypatch):
    upstream, clone = clone_behind(tmp_path)
    monkeypatch.setattr(_git, "fetch", lambda *a, **kw: pytest.fail("fetched without fetch: true"))
    head, tip = sh(clone, "rev-parse", "HEAD").strip(), sh(upstream, "rev-parse", "HEAD").strip()
    assert cursor_of(run_git(clone)) == {"commit": head, "ref": "HEAD", "base": "origin/main", "behind": 0}
    sh(clone, "fetch", "-q", "origin")  # origin/main moves one commit on; HEAD stays
    assert cursor_of(run_git(clone)) == {"commit": head, "ref": "HEAD", "base": "origin/main", "behind": 1}
    read = cursor_of(run_git(clone, ref="origin/main"))
    assert read == {"commit": tip, "ref": "origin/main", "base": "origin/main", "behind": 0}


def test_git_cursor_says_when_the_commit_was_read(tmp_path):
    _, clone = clone_behind(tmp_path)
    before = dt.datetime.now(dt.UTC).replace(microsecond=0)
    cursor = next(m for m in run_git(clone) if m["type"] == "state")["cursor"]
    at = dt.datetime.strptime(cursor["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    assert before <= at <= dt.datetime.now(dt.UTC)


def test_git_cursor_without_a_remote_branch_names_only_the_commit(tmp_path):
    repo = make_repo(tmp_path / "app")  # an origin it never fetched: no origin/HEAD, no origin/main
    assert cursor_of(run_git(repo)) == {"commit": sh(repo, "rev-parse", "HEAD").strip(), "ref": "HEAD"}


def test_kg_node_links_to_the_commit_read_while_items_keep_their_branch_link(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    sync_project(project)
    assert build_project(project).ok
    first = git(app, "rev-parse", "HEAD").strip()
    blob = "https://github.com/acme/demo-app/blob"
    corpus = project.corpus()
    assert {k: v for k, v in corpus.cursor("app").items() if k not in ("input", "at")} == {
        "commit": first,
        "ref": "HEAD",
        "url": f"https://github.com/acme/demo-app/commit/{first}",
        "blob": {"branch": f"{blob}/main/", "commit": f"{blob}/{first}/"},
    }
    item = corpus.get("app:file:app/search.py")
    assert item.record["uri"] == f"{blob}/main/app/search.py"  # by branch: the hash moves only with the file

    session = Session(project, "wide")
    for node_id in ("app:file:app/search.py", "app:app/search.py::search"):
        found = session.call("kg_node", {"id": node_id})
        evidence = found["structuredContent"]["evidence"]
        assert evidence and {e["uri"] for e in evidence} == {f"{blob}/{first}/app/search.py"}, node_id
        assert f" {blob}/{first}/app/search.py" in found["content"][0]["text"]
    node = session.call("kg_node", {"id": "app:file:app/search.py"})["structuredContent"]["node"]
    assert node["props"]["uri"] == f"{blob}/{first}/app/search.py"
    found = session.call("kg_search", {"query": "app/search.py"})["structuredContent"]["results"]
    assert {n["props"].get("uri") for n in found if n["id"] == "app:file:app/search.py"} == {item.record["uri"]}

    write(app / "docs/guide.md", "# Guide\n\nRewritten.\n")
    commit(app, "guide only")
    sync_project(project)
    assert build_project(project).ok
    second = git(app, "rev-parse", "HEAD").strip()
    assert project.corpus().get("app:file:app/search.py").hash == item.hash  # a new commit alone changes no item
    found = Session(project, "wide").call("kg_node", {"id": "app:file:app/search.py"})
    assert found["structuredContent"]["node"]["props"]["uri"] == f"{blob}/{second}/app/search.py"


def test_git_without_fetch_reads_the_ref_as_the_clone_has_it(tmp_path, monkeypatch):
    _, clone = clone_behind(tmp_path)
    monkeypatch.setattr(_git, "fetch", lambda *a, **kw: pytest.fail("fetched without fetch: true"))
    msgs = run_git(clone, ref="origin/main")
    assert "docs/new.md" not in items_by_path(msgs)
    assert msgs[-1]["status"] == "ok"


def test_git_fetch_failure_is_a_warning_and_the_run_stays_ok(tmp_path):
    _, clone = clone_behind(tmp_path)
    sh(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    msgs = run_git(clone, ref="origin/main", fetch=True)
    warnings = [m for m in msgs if m["type"] == "log" and m["level"] == "warning"]
    assert len(warnings) == 1 and "fetch failed" in warnings[0]["message"]
    assert "docs/guide.md" in items_by_path(msgs) and "docs/new.md" not in items_by_path(msgs)
    assert msgs[-1]["status"] == "ok"


def test_git_fetch_ignores_a_ref_that_is_not_an_origin_branch(tmp_path, monkeypatch):
    _, clone = clone_behind(tmp_path)
    monkeypatch.setattr(_git, "fetch", lambda *a, **kw: pytest.fail("fetched for a local ref"))
    msgs = run_git(clone, ref="HEAD", fetch=True)
    assert any(m["type"] == "log" and "ignored" in m["message"] for m in msgs)
    assert msgs[-1]["status"] == "ok"


def test_sync_reports_the_fetch_warning_on_an_ok_run(tmp_path, kg_env):
    _, clone = clone_behind(tmp_path)
    sh(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    root = tmp_path / "fetch-harness"
    root.mkdir()
    (root / "harness.yaml").write_text("name: proj\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (root / "knowledge.yaml").write_text(
        f"""
version: 1
project: proj
policy:
  levels: [internal]
  sinks:
    - id: test-session
      kind: agent-session
      clearance: {{level: internal}}
sources:
  - id: app
    connector: git
    path: '{clone}'
    ref: origin/main
    fetch: true
    label: {{level: internal, integrity: U}}
"""
    )
    (result,) = sync_project(load_project_at(root, kg_env))
    assert result.ok
    assert len(result.warnings) == 1 and "fetch failed" in result.warnings[0]


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


# The history of the ref and the pull requests of a repo on GitHub


def commits_of(messages) -> dict[str, dict]:
    return {m["props"]["sha"]: m for m in messages if m["type"] == "item" and m["kind"] == "commit"}


def listing_of(messages, scope: str) -> dict:
    return next(m for m in messages if m["type"] == "listing" and m["scope"] == scope)


def test_git_history_gives_each_commit_the_files_it_changed(tmp_path):
    repo = make_repo(tmp_path / "app")
    first = sh(repo, "rev-parse", "HEAD").strip()
    sh(repo, "checkout", "-qb", "feature")
    (repo / "config.yaml").write_text("name: app2\n")
    (repo / "node_modules/y.js").write_text("ignored too")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-qm", "feature: edit the config")
    sh(repo, "checkout", "-q", "main")
    (repo / "docs/guide.md").unlink()
    sh(repo, "commit", "-qam", "drop the guide")
    sh(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    merge, feature = sh(repo, "rev-parse", "HEAD").strip(), sh(repo, "rev-parse", "feature").strip()

    msgs = run_git(repo)
    commits = commits_of(msgs)
    assert len(commits) == 4
    assert commits[first]["props"]["paths"] == ["app/server.py", "config.yaml", "docs/guide.md", "logo.png"]
    assert commits[feature]["props"]["paths"] == ["config.yaml"]  # node_modules/ is excluded, as from the tree
    merged = commits[merge]
    assert merged["props"]["paths"] == ["config.yaml"] and merged["props"]["merge"] is True  # against main's side
    assert merged["id"] == f"app:commit:{merge}" and merged["rev"] == merge and merged["title"] == f"app@{merge[:10]}"
    assert merged["props"]["parents"][1] == feature and merged["props"]["files"] == 1
    assert merged["body"] == {"format": "text", "text": "Merge feature"} and merged["label"] == {"integrity": "U"}
    assert commits[feature]["body"]["text"] == "feature: edit the config"  # the message is the item's text
    assert "uri" not in merged  # a source by path has no forge to link to
    assert listing_of(msgs, "app:commit:") == {"type": "listing", "scope": "app:commit:", "complete": True, "count": 4}


def test_git_history_caps_the_paths_of_a_sweep_and_counts_them_all(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "app")
    monkeypatch.setattr(git_connector, "MAX_COMMIT_PATHS", 2)
    head = sh(repo, "rev-parse", "HEAD").strip()
    props = commits_of(run_git(repo))[head]["props"]
    assert props["paths"] == ["app/server.py", "config.yaml"] and props["files"] == 4


def commit_as(repo: Path, author: str, path: str, text: str) -> str:
    """Commit ``text`` to ``path`` with ``author`` (``Name <email>``) as its author; its SHA."""
    write(repo / path, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"edit {path}", "--author", author)
    return git(repo, "rev-parse", "HEAD").strip()


def test_commit_author_email_is_read_from_the_history_and_kept_on_the_commit_node(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env, app_level="customer")
    app = tmp_path / "demo-app"
    init = git(app, "rev-parse", "HEAD").strip()
    lumen = commit_as(app, "Lumen Writer <Lumen.Writer@Northwind.test>", "docs/notes.md", "# Notes\n")
    nobody = commit_as(app, "Nobody <>", "docs/notes.md", "# Notes\n\nMore.\n")

    msgs = run_git(app)
    emails = {sha: item["props"].get("author_email") for sha, item in commits_of(msgs).items()}
    # As the commit records it, case and all: the hub compares addresses without case.
    assert emails == {init: "t@example.test", lumen: "Lumen.Writer@Northwind.test", nobody: None}

    assert sync_project(project, ["app"])[0].ok
    report = build_project(project)
    assert report.ok, report.errors
    store = Store.for_project(project)
    node = store.node(f"app:commit:{lumen}", report.build_id)
    assert node["kind"] == "Commit" and node["props"]["author_email"] == "Lumen.Writer@Northwind.test"
    assert "author_email" not in store.node(f"app:commit:{nobody}", report.build_id)["props"]
    # Labelled as its source, like every other prop: the level of the git source, the label of its files.
    assert node["label"] == store.node("app:file:docs/notes.md", report.build_id)["label"]
    assert node["label"][0] == 2  # customer, the third of the policy's levels
    wide = Session(project, "wide").call("kg_node", {"id": f"app:commit:{lumen}"})
    assert wide["structuredContent"]["node"]["props"]["author_email"] == "Lumen.Writer@Northwind.test"
    narrow = Session(project, "agent").call("kg_node", {"id": f"app:commit:{lumen}"})
    assert narrow.get("isError") and "Northwind" not in json.dumps(narrow)


def test_commit_author_email_reaches_the_commits_in_the_corpus_at_the_first_sync_of_the_new_build(
    tmp_path, kg_env, monkeypatch
):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    lumen = commit_as(app, "Lumen <lumen@northwind.test>", "docs/notes.md", "# Notes\n")
    init = git(app, "rev-parse", "HEAD~1").strip()
    read = _git.history

    def history_before(*args):
        return [{k: v for k, v in found.items() if k != "author_email"} for found in read(*args)]

    with monkeypatch.context() as before:  # the build before this one: no author's email, and its own code digest
        before.setattr(_git, "history", history_before)
        before.setattr(git_connector, "_code", lambda: "sha256:the build before")
        assert sync_project(project, ["app"])[0].ok
        old = build_project(project)
    assert old.ok, old.errors
    commits = [r for r in project.corpus().items("app") if ":commit:" in r.item_id]
    assert len(commits) == 2 and all("author_email" not in r.record["props"] for r in commits)
    assert "author_email" not in Store.for_project(project).node(f"app:commit:{lumen}", old.build_id)["props"]

    (result,) = sync_project(project, ["app"])  # the new build's code digest: the whole history again
    assert result.ok and not result.unchanged and result.removals == 0 and result.held == [], result.issues
    corpus = project.corpus()
    emails = {sha: corpus.get(f"app:commit:{sha}").record["props"].get("author_email") for sha in (init, lumen)}
    assert emails == {init: "t@example.test", lumen: "lumen@northwind.test"}
    new = build_project(project, verify=True, cold=True)  # a cold rebuild of the same corpus gives the same hash
    assert new.ok and new.verify["match"], new.verify
    assert Store.for_project(project).node(f"app:commit:{lumen}", new.build_id)["props"]["author_email"] == (
        "lumen@northwind.test"
    )

    assert sync_project(project, ["app"])[0].unchanged  # the cursor holds the new input: nothing read again
    again = build_project(project, force=True)
    assert again.ok and again.content_hash == new.content_hash


def test_git_history_after_a_cursor_of_the_same_input_reads_only_the_new_commits(tmp_path):
    repo = make_repo(tmp_path / "app")
    source = {"id": "app", "connector": "git", "path": str(repo)}
    first = list(ConnectorRun(ConnectorContext("p", source)))
    cursor = next(m for m in first if m["type"] == "state")["cursor"]
    (repo / "config.yaml").write_text("name: app2\n")
    sh(repo, "commit", "-qam", "edit")
    head = sh(repo, "rev-parse", "HEAD").strip()

    later = list(ConnectorRun(ConnectorContext("p", source, cursor=cursor)))
    assert list(commits_of(later)) == [head] and warnings_of(later) == []
    assert listing_of(later, "app:commit:")["complete"] is False  # what the first run read stays
    other = list(ConnectorRun(ConnectorContext("p", source, cursor={**cursor, "input": "sha256:other code"})))
    assert len(commits_of(other)) == 2 and listing_of(other, "app:commit:")["complete"] is True
    gone = list(ConnectorRun(ConnectorContext("p", source, cursor={**cursor, "commit": "0" * 40})))
    assert len(commits_of(gone)) == 2 and gone[-1]["status"] == "ok"  # a commit the clone lacks: the whole history
    assert listing_of(gone, "app:commit:")["complete"] is True
    assert warnings_of(gone) == [
        "history of HEAD rewritten: 0000000000, the commit the last run read, is not in the clone; "
        "read the whole history, so the commits only on the old one go"
    ]


def warnings_of(messages) -> list[str]:
    return [m["message"] for m in messages if m["type"] == "log" and m["level"] == "warning"]


def rewrite(repo: Path, keep: int) -> None:
    """Give every commit of main after its first ``keep`` a new message, as filter-repo rewrites a history: new SHAs
    from there on, and the old commits still in the clone with no ref that reaches them."""
    shas = sh(repo, "rev-list", "--reverse", "main").split()
    parent = shas[keep - 1] if keep else None
    for sha in shas[keep:]:
        tree = sh(repo, "rev-parse", f"{sha}^{{tree}}").strip()
        parent = sh(repo, "commit-tree", tree, "-m", f"rewritten {sha[:10]}", *(["-p", parent] if parent else []))
        parent = parent.strip()
    sh(repo, "update-ref", "refs/heads/main", parent)


def test_git_history_is_read_whole_again_once_the_ref_no_longer_reaches_the_cursors_commit(tmp_path):
    repo = make_repo(tmp_path / "app")
    for n in range(3):
        (repo / "config.yaml").write_text(f"name: app{n}\n")
        sh(repo, "commit", "-qam", f"edit {n}")
    source = {"id": "app", "connector": "git", "path": str(repo)}
    first = list(ConnectorRun(ConnectorContext("p", source)))
    cursor = next(m for m in first if m["type"] == "state")["cursor"]
    rewrite(repo, keep=2)
    head = sh(repo, "rev-parse", "HEAD").strip()
    # The old tip is still an object of the clone, so `git log <old>..<new>` runs and lists the new commits only.
    assert sh(repo, "cat-file", "-t", cursor["commit"]).strip() == "commit"
    assert len(sh(repo, "rev-list", f"{cursor['commit']}..{head}").split()) == 2

    later = list(ConnectorRun(ConnectorContext("p", source, cursor=cursor)))
    assert set(commits_of(later)) == set(sh(repo, "rev-list", "HEAD").split())
    assert len(set(commits_of(later)) & set(commits_of(first))) == 2  # the two kept, then the two rewritten
    assert listing_of(later, "app:commit:") == {"type": "listing", "scope": "app:commit:", "complete": True, "count": 4}
    assert warnings_of(later) == [
        f"history of HEAD rewritten: {cursor['commit'][:10]}, the commit the last run read, is not an ancestor of "
        f"{head[:10]}; read the whole history, so the commits only on the old one go"
    ]
    assert later[-1]["status"] == "ok"

    cursor = next(m for m in later if m["type"] == "state")["cursor"]
    sh(repo, "reset", "-q", "--hard", "HEAD~1")  # back to an older commit: nothing new to read, one commit to drop
    back = list(ConnectorRun(ConnectorContext("p", source, cursor=cursor)))
    assert len(commits_of(back)) == 3 and listing_of(back, "app:commit:")["complete"] is True
    assert len(warnings_of(back)) == 1


def commits_in(corpus) -> set[str]:
    return {r.item_id.rpartition(":")[2] for r in corpus.items("app") if ":commit:" in r.item_id}


def test_sync_after_a_rewrite_removes_the_old_commits_and_holds_a_rewrite_of_too_many(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    for n in range(11):
        write(app / "docs/notes.md", f"# Notes\n\nVersion {n} of the notes.\n")
        commit(app, f"notes {n}")
    assert sync_project(project, ["app"])[0].ok
    assert len(commits_in(project.corpus())) == 12
    files = {r.item_id: r.rev for r in project.corpus().items("app") if ":file:" in r.item_id}

    old = git(app, "rev-parse", "HEAD").strip()
    git(app, "commit", "-q", "--amend", "-m", "notes 10, reworded")  # a force-push of the last commit
    (result,) = sync_project(project, ["app"])
    assert result.ok and result.removals == 1 and result.held == [], result.issues
    assert len(result.warnings) == 1 and result.warnings[0].startswith(f"history of HEAD rewritten: {old[:10]}")
    corpus = project.corpus()
    assert commits_in(corpus) == set(git(app, "rev-list", "HEAD").split())  # 12, the old tip gone
    assert corpus.get(f"app:commit:{old}").deleted

    rewrite(app, keep=4)  # eight of the twelve commits get new SHAs
    (result,) = sync_project(project, ["app"])
    assert result.ok and result.removals == 0 and len(result.warnings) == 1, result.issues
    reason = "would remove 8 of 12 item(s), above max_removal_ratio 0.3"
    assert result.held == [{"scope": "app:commit:", "reason": reason, "count": 8}]
    new = set(git(app, "rev-list", "HEAD").split())
    corpus = project.corpus()
    assert len(commits_in(corpus)) == 20 and new <= commits_in(corpus)  # the old ones wait for a person
    assert [(h["scope"], len(h["ids"])) for h in corpus.held("app")] == [("app:commit:", 8)]
    assert {r.item_id: r.rev for r in corpus.items("app") if ":file:" in r.item_id} == files  # the same trees

    assert accept_removals(project, "app") == 8
    corpus = project.corpus()
    assert commits_in(corpus) == new and corpus.held("app") == []
    assert sync_project(project, ["app"])[0].unchanged  # the cursor names the new tip: nothing to read


TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # made up: what a token looks like, never one


def github_pull(number: int, updated: str, **extra) -> dict:
    return {
        "number": number,
        "title": f"Change {number}",
        "state": "closed",
        "merged_at": None,
        "merge_commit_sha": "f" * 40,
        "head": {"ref": f"feat/{number}", "sha": "e" * 40},
        "base": {"ref": "main"},
        "html_url": f"https://github.com/acme/demo-app/pull/{number}",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": updated,
        "body": "Free text of the pull request, read for the codes it names.",
        **extra,
    }


@pytest.fixture
def github(monkeypatch):
    """GitHub stood in for: the pages it answers, and the requests it got as (repo, page)."""
    fake = type("FakeGitHub", (), {})()
    fake.pages, fake.asked = [], []

    def page(project, number, secret):
        assert secret == TOKEN
        fake.asked.append((project, number))
        return fake.pages[number - 1] if number <= len(fake.pages) else []

    monkeypatch.setattr(github_api, "_page", page)
    monkeypatch.setattr(github_api, "enabled", lambda env=None: True)
    monkeypatch.setattr(github_api, "token", lambda env=None: TOKEN)
    return fake


def test_git_reads_the_pull_requests_of_a_repo_on_github_with_their_body_but_no_comment(tmp_path, kg_env, github):
    project = make_project(tmp_path, kg_env)
    head = git(tmp_path / "demo-app", "rev-parse", "HEAD").strip()
    merged = github_pull(7, "2026-10-02T00:00:00Z", merged_at="2026-10-02T00:00:00Z", merge_commit_sha=head)
    github.pages = [[merged, github_pull(6, "2026-10-01T00:00:00Z", state="open")]]
    (result,) = sync_project(project, ["app"])
    assert result.ok, result.issues
    assert github.asked == [("acme/demo-app", 1)]
    corpus = project.corpus()
    pull = corpus.get("app:pr:7").record
    assert pull["kind"] == "pr" and pull["title"] == "Change 7" and pull["label"] == {"integrity": "U"}
    assert pull["uri"] == "https://github.com/acme/demo-app/pull/7"
    assert corpus.body_text(pull) == "Free text of the pull request, read for the codes it names."
    assert pull["props"] == {
        "repo": "app",
        "github": "acme/demo-app",
        "number": 7,
        "status": "merged",
        "head": "feat/7",
        "base": "main",
        "merge_commit": head,
    }
    assert (
        corpus.get("app:pr:6").record["props"]["status"] == "open"
        and "merge_commit" not in corpus.get("app:pr:6").record["props"]
    )
    assert corpus.cursor("app")["pulls"] == {"since": "2026-10-02T00:00:00Z"}
    written = gzip.decompress(Path(result.log).read_bytes()).decode()
    assert "Free text" not in written and TOKEN not in written  # neither the body nor the token reach the log
    assert TOKEN not in json.dumps(corpus.cursor("app"))

    report = build_project(project)
    assert report.ok, report.errors
    store = Store.for_project(project)
    node = store.node("app:pr:7", report.build_id)
    assert node["kind"] == "PullRequest" and node["name"] == "Change 7"
    assert {k: node["props"][k] for k in ("number", "status", "head", "base")} == {
        "number": 7,
        "status": "merged",
        "head": "feat/7",
        "base": "main",
    }
    edges = {(e["src"], e["rel"], e["dst"]) for e in store.edges_of("app:pr:7", report.build_id)}
    assert ("app:pr:7", "merged_as", f"app:commit:{head}") in edges
    text = Session(project, "wide").call("kg_node", {"id": "app:pr:7"})["content"][0]["text"]
    assert "Free text" not in text and "(parsed, internal,U)" in text.splitlines()[0]


def test_git_reads_only_the_pull_requests_updated_since_the_newest_it_saw(tmp_path, kg_env, github):
    project = make_project(tmp_path, kg_env)
    github.pages = [[github_pull(7, "2026-10-02T00:00:00Z"), github_pull(6, "2026-10-01T00:00:00Z")]]
    assert sync_project(project, ["app"])[0].ok
    write(tmp_path / "demo-app/docs/new.md", "# New\n")
    commit(tmp_path / "demo-app")
    github.asked.clear()
    github.pages = [
        [github_pull(8, "2026-10-03T00:00:00Z"), github_pull(7, "2026-10-02T00:00:00Z")],
        [github_pull(5, "x")],
    ]
    (result,) = sync_project(project, ["app"])
    assert result.ok and result.items - result.removals > 0
    assert github.asked == [("acme/demo-app", 1)]  # page 1 ends below a full page, so page 2 is never asked for
    assert project.corpus().cursor("app")["pulls"] == {"since": "2026-10-03T00:00:00Z"}
    assert {r.item_id for r in project.corpus().items("app") if ":pr:" in r.item_id} == {
        "app:pr:6",
        "app:pr:7",
        "app:pr:8",
    }


def test_git_run_stays_ok_when_github_cannot_be_read_and_keeps_its_cursor(tmp_path, kg_env, github, monkeypatch):
    project = make_project(tmp_path, kg_env)
    github.pages = [[github_pull(7, "2026-10-02T00:00:00Z")]]
    assert sync_project(project, ["app"])[0].ok

    def refused(project, number, secret):
        raise github_api.PullsError(f"GitHub answered HTTP 401 for the pull requests of {project}")

    monkeypatch.setattr(github_api, "_page", refused)
    write(tmp_path / "demo-app/docs/new.md", "# New\n")
    commit(tmp_path / "demo-app")
    (result,) = sync_project(project, ["app"])
    assert result.ok and result.warnings == [
        "pulls not read: GitHub answered HTTP 401 for the pull requests of acme/demo-app"
    ]
    assert project.corpus().cursor("app")["pulls"] == {"since": "2026-10-02T00:00:00Z"}
    assert not project.corpus().get("app:pr:7").deleted  # a pull request read before stays


def history_of(corpus) -> set[str]:
    return {r.item_id for r in corpus.items("app") if ":commit:" in r.item_id or ":pr:" in r.item_id}


def items_in(path: Path) -> set[str]:
    return {record["id"] for record in Corpus.read_log(path) if record.get("type") == "item"}


def test_a_default_prune_keeps_the_commits_and_pull_requests_a_later_run_reads_on_from(tmp_path, kg_env, github):
    """Later runs read only the commits and pull requests since the cursor's (``history_and_pulls``): the records of
    the runs before must outlive retention (``evo_agents.kg.retention``), or the graph would lose them for good."""
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    github.pages = [[github_pull(7, "2026-10-02T00:00:00Z"), github_pull(6, "2026-10-01T00:00:00Z")]]
    runs = [sync_project(project, ["app"])[0]]
    github.pages = []  # no pull request updated since
    for version in range(2):
        write(app / "docs/notes.md", f"# Notes\n\nVersion {version} of the notes.\n")
        commit(app, f"notes {version}")
        runs.append(sync_project(project, ["app"])[0])
    assert all(r.ok for r in runs)
    first, second, third = (Path(r.log) for r in runs)
    corpus = project.corpus()
    history = history_of(corpus)
    assert len(history) == 5  # three commits, two pull requests, each read by one run only
    state, cursor = corpus.state_digest(), corpus.cursor("app")

    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=2)  # every run is older than the default periods
    found = retention.plan(project.name, project.root, retention.DEFAULTS, now=later, policy=project.policy)
    # The third run holds every file; the two before hold nothing else the corpus has but their history.
    assert found.logs == [] and set(found.rewrite) == {first.name, second.name}
    assert found.kept[retention.IN_USE] == 1 and found.runs == []
    retention.apply(found)
    assert items_in(first) | items_in(second) == history - {f"app:commit:{git(app, 'rev-parse', 'HEAD').strip()}"}
    corpus = project.corpus()
    assert [p.name for p in corpus.log_files()] == [first.name, second.name, third.name]
    assert history_of(corpus) == history
    assert corpus.state_digest(corpus.rebuild_state()) == state == corpus.state_digest()
    assert corpus.cursor("app") == cursor

    write(app / "docs/notes.md", "# Notes\n\nVersion 2 of the notes.\n")
    commit(app, "notes 2")
    head = git(app, "rev-parse", "HEAD").strip()
    github.pages = [[github_pull(8, "2026-10-03T00:00:00Z")]]
    (result,) = sync_project(project, ["app"])
    assert result.ok, result.issues
    read = list(Corpus.read_log(Path(result.log)))
    assert list(commits_of(read)) == [head] and listing_of(read, "app:commit:")["complete"] is False
    assert {m["id"] for m in read if m.get("type") == "item" and m["kind"] == "pr"} == {"app:pr:8"}
    corpus = project.corpus()
    assert history_of(corpus) == history | {f"app:commit:{head}", "app:pr:8"}  # nothing read before was lost
    report = build_project(project, verify=True, cold=True)
    assert report.ok and report.verify["match"], report.verify


def test_git_reads_no_pull_request_without_a_token_or_when_turned_off(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "app")
    harness = Harness(
        tmp_path, {"name": "h", "repos": [{"name": "app", "path": str(repo), "origin": "git@github.com:acme/app.git"}]}
    )
    monkeypatch.setattr(github_api, "_page", lambda *a: pytest.fail("asked GitHub"))
    monkeypatch.setattr(github_api, "enabled", lambda env=None: True)
    monkeypatch.setattr(github_api, "token", lambda env=None: None)
    msgs = list(ConnectorRun(ConnectorContext("p", {"id": "app", "connector": "git", "repo": "app"}, harness)))
    assert msgs[-1]["status"] == "ok" and not any(m.get("kind") == "pr" for m in msgs)
    assert any(m["type"] == "log" and m["level"] == "info" and "no GitHub token" in m["message"] for m in msgs)
    monkeypatch.setattr(github_api, "token", lambda env=None: pytest.fail("looked for a token"))
    source = {"id": "app", "connector": "git", "repo": "app", "pulls": False}
    assert list(ConnectorRun(ConnectorContext("p", source, harness)))[-1]["status"] == "ok"


def test_github_token_comes_from_the_source_then_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert github_api.token({"GH_TOKEN": "from-source"}) == "from-source"
    assert github_api.token({}) == "from-env"
    assert github_api.enabled({}) is False  # the suite turns the reading off (tests/conftest.py)
    assert github_api.enabled({"EVO_KG_PULLS": "1"}) is True


# The plans of the hub, in a harness whose harness.yaml names a hub project


HUB_BODY = {"id": "p", "steps": [{"id": 1, "what": "x", "status": "done"}]}
HUB_TEXT = (
    "# Mirror of evo-agents hub plan p, revision 4. Do not edit\n"
    "id: p\nsteps:\n  - {id: 1, what: x, status: done}\n"
    f"hub: {{project: h, revision: 4, digest: '{plan_digest(HUB_BODY)}'}}\n"
)


def hub_reader(answer, calls: list | None = None):
    def read(harness, project, held):
        if calls is not None:
            calls.append((project, held))
        return answer(project) if callable(answer) else answer

    return read


def hub_harness(harness_dir: Path) -> Harness:
    (harness_dir / "harness.yaml").write_text("name: h\nrepos: []\nhub: {project: h}\n")
    (harness_dir / "plans/active/draft.yaml").write_text("id: draft\nsteps: []\n")  # not on the hub
    return Harness(harness_dir, {"name": "h", "hub": {"project": "h"}})


def plan_entry(area: str = "completed") -> dict:
    return {
        "plan_id": "p",
        "area": area,
        "revision": 4,
        "digest": plan_digest(HUB_BODY),
        "label": {"level": "internal", "location": "any", "projects": ["h"], "integrity": "U"},
        "text": HUB_TEXT,
        "updated_at": "2026-10-18T15:59:04.611428Z",
        "url": "https://hub.test/p/h/plans/p",
    }


def held_of(entry: dict) -> dict:
    return {key: entry[key] for key in ("area", "revision", "digest", "label", "updated_at", "url")}


HARNESS = {"id": "harness", "connector": "harness"}


def test_harness_connector_reads_the_plans_the_hub_holds_instead_of_their_copies(harness_dir):
    harness = hub_harness(harness_dir)
    calls: list = []
    cursor = {"plans": {"project": "h", "held": {"p": {"revision": 3}}}}
    reader = hub_reader({"plans": [plan_entry()]}, calls)
    msgs = list(ConnectorRun(ConnectorContext("p", HARNESS, harness, cursor, plans=reader)))
    assert calls == [("h", {"p": {"revision": 3}})]  # what the run before held, for this project
    items = items_by_path(msgs)
    assert "plans/active/p.yaml" not in items  # the copy on disk is an older revision, in the other area
    plan = items["plans/completed/p.yaml"]
    assert plan["kind"] == "plan" and plan["body"]["text"] == HUB_TEXT
    assert plan["label"] == {"level": "internal", "location": "any", "integrity": "U"}  # kg/1 labels name no project
    assert plan["uri"] == "https://hub.test/p/h/plans/p" and plan["rev_time"] == "2026-10-18T15:59:04.611428Z"
    assert items["plans/active/draft.yaml"]["kind"] == "plan"  # a plan the hub does not hold comes from disk
    state = next(m for m in msgs if m["type"] == "state")
    assert state["cursor"] == {"plans": {"project": "h", "held": {"p": held_of(plan_entry())}}}
    assert msgs[-2] == {"type": "listing", "scope": "harness:file:", "complete": True, "count": len(items)}
    assert msgs[-1] == {"type": "closed", "status": "ok"}


def test_a_copy_of_the_revision_the_cursor_holds_gives_the_same_item_without_the_hub(harness_dir):
    harness = hub_harness(harness_dir)
    entry = plan_entry("active")
    with_hub = list(ConnectorRun(ConnectorContext("p", HARNESS, harness, plans=hub_reader({"plans": [entry]}))))
    cursor = next(m for m in with_hub if m["type"] == "state")["cursor"]
    (harness_dir / "plans/active/p.yaml").write_text(HUB_TEXT)  # exported since: a later file time changes nothing
    for reader in (None, hub_reader(None), hub_reader({"error": "the hub is down"})):
        without = list(ConnectorRun(ConnectorContext("p", HARNESS, harness, cursor, plans=reader)))
        assert items_by_path(without) == items_by_path(with_hub)
        assert not any(m["type"] == "state" for m in without)  # the cursor stays as the hub left it
    (harness_dir / "plans/active/p.yaml").write_text(HUB_TEXT.replace("status: done", "status: pending"))
    edited = items_by_path(list(ConnectorRun(ConnectorContext("p", HARNESS, harness, cursor))))
    assert "label" not in edited["plans/active/p.yaml"]  # no longer the revision held: read as any file


def test_harness_connector_reads_the_copies_without_a_hub_and_warns_when_the_hub_fails(harness_dir):
    harness = hub_harness(harness_dir)
    disk = items_by_path(list(ConnectorRun(ConnectorContext("p", HARNESS, harness))))
    copy = (harness_dir / "plans/active/p.yaml").read_bytes().decode()  # its lines end in CRLF on Windows
    assert copy.replace("\r\n", "\n") == "id: p\nsteps: []\n"
    assert disk["plans/active/p.yaml"]["body"]["text"] == copy and "label" not in disk["plans/active/p.yaml"]

    signed_out = list(ConnectorRun(ConnectorContext("p", HARNESS, harness, plans=hub_reader(None))))
    assert items_by_path(signed_out) == disk and not any(m["type"] in ("state", "log") for m in signed_out)

    for answer in ({"error": "the plans of hub project h were not read (HTTP 503)"}, lambda project: 1 / 0):
        msgs = list(ConnectorRun(ConnectorContext("p", HARNESS, harness, plans=hub_reader(answer))))
        assert items_by_path(msgs) == disk and msgs[-1]["status"] == "ok"
        (warning,) = [m for m in msgs if m["type"] == "log"]
        assert warning["level"] == "warning" and "the plans of hub project h were not read" in warning["message"]

    plain = Harness(harness_dir, {"name": "h"})  # no hub project: the reader is never asked
    reader = hub_reader(lambda project: pytest.fail("asked the hub"))
    assert items_by_path(list(ConnectorRun(ConnectorContext("p", HARNESS, plain, plans=reader)))) == disk
