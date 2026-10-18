"""A source that starts reading from somewhere else (another git ref, repo or path) supersedes what it read before,
whatever the commit times say."""

import os
import random
import sqlite3
import subprocess
from pathlib import Path

from evo_agents.kg.corpus import BASE_EPOCH, Corpus
from evo_agents.kg.ids import uuid7
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import accept_removals, source_lineage, sync_source
from tests.kg.conftest import doc


def git(repo: Path, *args: str, date: str = "2026-09-01T09:00:00Z") -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def commit(repo: Path, date: str, files: dict[str, str | None]) -> None:
    for path, text in files.items():
        target = repo / path
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"at {date}", date=date)


def two_branches(root: Path) -> Path:
    """A repo whose branch ``deploy`` forked from ``main`` and was last changed a month before ``main``."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit(
        root,
        "2026-09-01T09:00:00Z",
        {"docs/guide.md": "# Guide\n\nfirst\n", "app/server.py": "PORT = 1\n", "config.yaml": "name: app\n"},
    )
    git(root, "branch", "deploy")
    commit(
        root,
        "2026-10-01T09:00:00Z",
        {
            "docs/guide.md": "# Guide\n\nmain\n",
            "app/server.py": "PORT = 3\n",
            "config.yaml": "name: app-main\n",
            "docs/main-only.md": "# Main only\n",
        },
    )
    git(root, "checkout", "-q", "deploy")
    commit(root, "2026-09-05T09:00:00Z", {"docs/guide.md": "# Guide\n\ndeploy\n", "docs/deploy-only.md": "# Deploy\n"})
    git(root, "checkout", "-q", "main")
    return root


def git_project(tmp_path: Path, kg_env: Path, repo: Path, ref: str, extra: str = ""):
    root = tmp_path / "harness"
    root.mkdir(exist_ok=True)
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
    path: '{repo}'
    ref: {ref}
    label: {{level: internal, integrity: U}}
{extra}"""
    )
    return load_project_at(root, kg_env)


def files_at(repo: Path, ref: str) -> dict[str, str]:
    paths = git(repo, "ls-tree", "-r", "--name-only", ref).split()
    return {path: git(repo, "show", f"{ref}:{path}") for path in paths}


def alive_texts(corpus: Corpus) -> dict[str, str]:
    return {r.record["props"]["path"]: corpus.body_text(r.record) for r in corpus.items("app")}


def test_switching_to_a_ref_with_older_commits_takes_its_content_for_every_file(tmp_path, kg_env):
    repo = two_branches(tmp_path / "repo")
    assert sync_source(git_project(tmp_path, kg_env, repo, "main"), "app").ok
    assert alive_texts(Corpus("proj", kg_env)) == files_at(repo, "main")

    project = git_project(tmp_path, kg_env, repo, "deploy")
    result = sync_source(project, "app")
    assert result.ok and result.removals == 1  # docs/main-only.md is not on deploy
    corpus = project.corpus()
    assert alive_texts(corpus) == files_at(repo, "deploy")
    assert corpus.get("app:file:docs/main-only.md").deleted

    expected = corpus.state_digest()
    rng = random.Random(3)
    for _ in range(4):
        order = corpus.log_files() * 2
        rng.shuffle(order)
        assert corpus.state_digest(corpus.rebuild_state(order)) == expected

    project = git_project(tmp_path, kg_env, repo, "main")  # and back: the newest switch wins again
    assert sync_source(project, "app").ok
    assert alive_texts(project.corpus()) == files_at(repo, "main")


def test_runs_of_one_lineage_share_its_epoch_and_a_new_lineage_starts_one(tmp_path, kg_env):
    repo = two_branches(tmp_path / "repo")
    first = sync_source(git_project(tmp_path, kg_env, repo, "main"), "app")
    commit(repo, "2026-10-02T09:00:00Z", {"docs/guide.md": "# Guide\n\nmain, later\n"})  # else nothing is read again
    again = sync_source(git_project(tmp_path, kg_env, repo, "main", extra="    refresh: 1h\n"), "app")
    switched = sync_source(git_project(tmp_path, kg_env, repo, "deploy"), "app")
    headers = [next(Corpus.read_log(Path(r.log))) for r in (first, again, switched)]
    assert [h["epoch"] for h in headers] == [BASE_EPOCH, BASE_EPOCH, switched.run_id]
    assert headers[0]["lineage"] == headers[1]["lineage"] != headers[2]["lineage"]


def test_lineage_follows_where_a_source_reads_from_not_how_it_is_run():
    base = {"id": "app", "connector": "git", "repo": "app", "ref": "origin/main"}
    same = {
        **base,
        "refresh": "1h",
        "fetch": True,
        "label": {"level": "internal"},
        "deletion": {"min_scope": 0},
        "include": ["*.md"],
    }
    assert source_lineage(same) == source_lineage(base)
    for changed in ({"ref": "origin/deploy/evobright"}, {"repo": "other"}, {"path": "/srv/app"}):
        assert source_lineage({**base, **changed}) != source_lineage(base)


def test_the_first_run_after_an_upgrade_supersedes_records_logged_without_an_epoch(fake_project):
    project, fake = fake_project
    corpus = project.corpus()
    legacy = uuid7()
    item = {"type": "item", "id": "docs:doc:a", "kind": "doc", "rev": "9", "rev_time": "2026-10-09T00:00:00Z"}
    with corpus.db:
        corpus.merge([{"type": "run", "run_id": legacy, "source": "docs"}, {**item, "hash": "sha256:old"}], legacy)
    fake.set(items=[doc("a", "# a\n\ncurrent\n", rev="2", rev_time="2026-10-01T00:00:00Z")])
    assert sync_source(project, "docs").ok
    assert project.corpus().get("docs:doc:a").rev == "2"


def test_removals_held_before_a_switch_are_accepted_in_the_new_epoch(fake_project):
    project, fake = fake_project
    fake.set(items=[doc(f"d{i:02}") for i in range(4)])
    sync_source(project, "docs")
    knowledge = project.knowledge_path
    knowledge.write_text(knowledge.read_text().replace("config: {file:", "config: {variant: 2, file:"))
    project = load_project_at(project.harness.root, project.home)
    fake.set(items=[doc("d00")], listing=True)
    result = sync_source(project, "docs")
    assert result.held and result.removals == 0
    assert accept_removals(project, "docs") == 3
    assert sorted(r.item_id for r in project.corpus().items()) == ["docs:doc:d00"]


def test_a_corpus_from_before_epochs_gains_the_column_and_keeps_its_items(kg_env):
    root = kg_env / "old"
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "corpus.sqlite")
    db.executescript(
        """
        CREATE TABLE items (item_id TEXT PRIMARY KEY, source TEXT NOT NULL, deleted INTEGER NOT NULL,
            k_time TEXT NOT NULL, k_rev TEXT NOT NULL, k_run TEXT NOT NULL, k_tie TEXT NOT NULL,
            record TEXT NOT NULL) STRICT;
        INSERT INTO items VALUES ('s:doc:a', 's', 0, '2026-10-01T00:00:00.000000Z', '', 'r1', 't', '{"id": "s:doc:a"}');
        """
    )
    db.commit()
    db.close()
    corpus = Corpus("old", kg_env)
    assert corpus.db.execute("SELECT k_epoch FROM items").fetchall() == [("",)]
    assert corpus.get("s:doc:a").record == {"id": "s:doc:a"}
    Corpus("old", kg_env).close()  # a second open finds the column there
