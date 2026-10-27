"""A mention of a use case's code in a commit message, a pull request's title and body or a comment of the code is an
edge to the node of the use case, the old number of a use case (an alias) included (plan srs-backlog-loop, a4)."""

from pathlib import Path

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.connectors import _git
from evo_agents.kg.connectors import github as github_api
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, make_project, write

CODE, ALIAS = "UC CCACT-372", "UC 41"
NODE = f"usecase:{CODE}"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # made up: what a token looks like, never one


@pytest.fixture
def github(monkeypatch):
    """GitHub stood in for: the pages it answers."""
    fake = type("FakeGitHub", (), {})()
    fake.pages = []
    monkeypatch.setattr(
        github_api, "_page", lambda project, number, secret: fake.pages[number - 1] if number <= len(fake.pages) else []
    )
    monkeypatch.setattr(github_api, "enabled", lambda env=None: True)
    monkeypatch.setattr(github_api, "token", lambda env=None: TOKEN)
    return fake


def pull(number: int, title: str, body: str) -> dict:
    return {
        "number": number,
        "title": title,
        "state": "closed",
        "merged_at": None,
        "head": {"ref": f"feat/{number}"},
        "base": {"ref": "main"},
        "html_url": f"https://github.com/acme/demo-app/pull/{number}",
        "updated_at": "2026-10-02T00:00:00Z",
        "body": body,
    }


def with_use_case_codes(base: Path, home: Path):
    """The project of ``make_project`` whose identifiers are the codes of the hub's use cases, as its source gives them:
    ``UC <area>-<n>`` and the old numbers of use cases, mapped to the code they stand for."""
    root = base / "demo-harness"
    path = root / "knowledge.yaml"
    text = path.read_text(encoding="utf-8")
    head = text[: text.index("identifiers:")]
    path.write_text(
        head
        + "identifiers:\n"
        + "  - kind: UseCase\n"
        + "    pattern: '(?<![\\w-])UC [A-Z]+-\\d+(?![\\w-])'\n"
        + "  - kind: UseCase\n"
        + "    pattern: '(?<![\\w-])(?:UC\\s+41)(?![\\w-])'\n"
        + f"    aliases: {{'{ALIAS}': '{CODE}'}}\n",
        encoding="utf-8",
    )
    return load_project_at(root, home)


def edges_of(project) -> set[tuple]:
    report = build_project(project)
    assert report.ok, report.errors
    store = Store.for_project(project)
    rows = store.db.execute(
        "SELECT src, rel, dst FROM edges WHERE tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)", {"b": report.build_id}
    )
    return {tuple(r) for r in rows}


def test_commit_messages_pull_requests_and_comments_mention_the_use_case(tmp_path, kg_env, github):
    make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "app/login.py", f"def login():\n    # {CODE}: sign the member in\n    return 1\n")
    write(
        app / "app/old.ts",
        f"/* {ALIAS} was the number of it\n   still named here */\nexport const x = 1;\n// no code\n",
    )
    commit(app, f"Sign in\n\nImplements {CODE}.")
    first = git(app, "rev-parse", "HEAD").strip()
    write(app / "app/more.py", "x = 1\n")
    commit(app, f"Keep going, {ALIAS} in the middle of a line")
    second = git(app, "rev-parse", "HEAD").strip()
    write(app / "app/none.py", "y = 2\n")
    commit(app, "Nothing to see")
    third = git(app, "rev-parse", "HEAD").strip()
    github.pages = [[pull(7, f"Login for {CODE}", "Body, also the old number UC 41."), pull(8, "Plain", "Nothing")]]
    project = with_use_case_codes(tmp_path, kg_env)
    (result,) = sync_project(project, ["app"])
    assert result.ok, result.issues

    edges = edges_of(project)
    assert ("app:commit:" + first, "mentions", NODE) in edges
    assert ("app:commit:" + second, "mentions", NODE) in edges  # the alias reaches the node of its code
    assert not [e for e in edges if e[0] == "app:commit:" + third and e[2].startswith("usecase:")]
    assert ("app:pr:7", "mentions", NODE) in edges
    assert not [e for e in edges if e[0] == "app:pr:8" and e[2].startswith("usecase:")]
    assert ("app:file:app/login.py", "mentions", NODE) in edges
    assert ("app:file:app/old.ts", "mentions", NODE) in edges
    assert not [e for e in edges if e[2] == f"usecase:{ALIAS}"]  # no node of the alias itself
    assert not [e for e in edges if e[0] == "app:file:app/more.py" and e[2].startswith("usecase:")]


def test_a_code_outside_a_comment_or_a_message_is_no_mention(tmp_path, kg_env):
    make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "app/plain.py", f'NAME = "{CODE}"\n')  # a string is not a comment
    commit(app, "Add a constant")
    project = with_use_case_codes(tmp_path, kg_env)
    assert sync_project(project, ["app"])[0].ok
    assert not [e for e in edges_of(project) if e[0] == "app:file:app/plain.py" and e[2].startswith("usecase:")]


def test_the_history_gives_each_commit_its_message(tmp_path):
    app = tmp_path / "demo-app"
    app.mkdir()
    git(app, "init", "-q", "-b", "main")
    write(app / "a.txt", "a\n")
    commit(app, "First line\n\nSecond paragraph.")
    write(app / "b.txt", "b\n")
    commit(app, "Second")
    found = _git.history(app, "HEAD")
    assert [(c["message"], c["paths"]) for c in found] == [
        ("Second", ["b.txt"]),
        ("First line\n\nSecond paragraph.", ["a.txt"]),
    ]
