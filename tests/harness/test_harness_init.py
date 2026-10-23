"""``evo-agents harness init`` (``evo_agents.harness.init``, plan autonomous-oss-growth, criterion a2): in a directory
it writes a minimal harness.yaml and knowledge.yaml for the git repos it finds there (the directory itself, or its
direct subdirectories), with each repo's origin and default branch read from git; `evo-agents harness validate`
accepts both, and `evo-agents hub project register` can send them; it overwrites no file and names the ones there; the
project is named by the directory unless --project names it. tests/hub/test_invites.py takes the same harness on to a
hub, where a holder of a register link registers it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.harness import load_yaml, validate_harness
from evo_agents.harness.init import LEVELS, Repo, knowledge, project_name, without_secret
from evo_agents.hub.registration import registration
from evo_agents.kg.policy import DEFAULT_LEVELS

LABEL = {"level": "internal", "integrity": "U"}
SINKS = [
    {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
    {"id": "evo-hub", "kind": "hub", "clearance": {"level": "internal"}},
]


def git_repo(directory: Path, branch: str, origin: str | None = None, origin_head: str | None = None) -> Path:
    """A git checkout at ``directory`` on ``branch``, with an origin and an ``origin/HEAD`` when given; no commit, no
    network: ``origin/HEAD`` is set as a clone sets it."""
    directory.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)

    git("init", "-q")
    git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
    if origin is not None:
        git("remote", "add", "origin", origin)
    if origin_head is not None:
        git("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{origin_head}")
    return directory


def init(capsys, *args: str):
    code = main(["harness", "init", *args])
    out, err = capsys.readouterr()
    return code, out, err


def no_issues(root: Path) -> None:
    reports = validate_harness(root)
    assert [report.kind for report in reports] == ["harness", "knowledge"]
    assert {report.path.name: report.issues for report in reports} == {"harness.yaml": [], "knowledge.yaml": []}


def test_harness_init_writes_what_harness_validate_accepts_for_the_repos_of_a_directory(tmp_path, capsys):
    workspace = tmp_path / "acme-tools"
    git_repo(workspace / "api", "main", "https://ci-bot:s3cret-pass@github.com/acme/api.git", origin_head="develop")
    git_repo(workspace / "web", "trunk")  # no origin
    git_repo(workspace / ".cache", "main")  # hidden: not a repo of the harness
    (workspace / "notes").mkdir()  # not a repo
    (workspace / "notes" / "todo.md").write_text("# todo\n", encoding="utf-8")

    code, out, err = init(capsys, str(workspace))
    assert (code, err) == (0, "")
    assert f"Wrote harness.yaml and knowledge.yaml in {workspace.resolve()} for project acme-tools" in out
    assert "api  origin https://github.com/acme/api.git, branch develop" in out
    assert "web  no origin, branch trunk" in out
    assert "evo-agents hub project register" in out

    assert load_yaml(workspace / "harness.yaml") == {
        "version": 1,
        "name": "acme-tools",
        "knowledge_file": "knowledge.yaml",
        "hub": {"project": "acme-tools"},
        "repos": [
            {
                "name": "api",
                "path": "acme-tools/api",
                "origin": "https://github.com/acme/api.git",
                "default_branch": "develop",
            },
            {"name": "web", "path": "acme-tools/web", "default_branch": "trunk"},
        ],
    }
    assert load_yaml(workspace / "knowledge.yaml") == {
        "version": 1,
        "project": "acme-tools",
        "policy": {"levels": DEFAULT_LEVELS, "sinks": SINKS},
        "sources": [
            {"id": "harness", "connector": "harness", "exclude": ["api/*", "web/*"], "label": LABEL},
            {"id": "api", "connector": "git", "repo": "api", "ref": "origin/develop", "fetch": True, "label": LABEL},
            {"id": "web", "connector": "git", "repo": "web", "ref": "trunk", "label": LABEL},
        ],
    }
    for name in ("harness.yaml", "knowledge.yaml"):
        text = (workspace / name).read_text(encoding="utf-8")
        assert text.startswith("# Written by `evo-agents harness init`") and "s3cret-pass" not in text
    assert "label: {level: internal, integrity: U}" in (workspace / "knowledge.yaml").read_text(encoding="utf-8")
    no_issues(workspace)
    assert main(["harness", "validate", str(workspace)]) == 0
    assert "0 with errors" in capsys.readouterr().out

    # what `evo-agents hub project register` sends from it: the repos, and a hub sink that takes memories and plans
    found = registration(workspace)
    assert found.project == "acme-tools" and found.push_warning() is None
    assert found.body["levels"] == DEFAULT_LEVELS and found.body["default_label"]["level"] == "internal"
    assert [(r["name"], r["origin"], r["default_branch"], r["path"]) for r in found.body["repos"]] == [
        ("api", "https://github.com/acme/api.git", "develop", "acme-tools/api"),
        ("web", None, "trunk", "acme-tools/web"),
    ]
    assert found.body["harness"]["name"] == "acme-tools" and found.body["harness"]["path"] == "acme-tools"


def test_harness_init_in_a_repo_names_the_project_after_its_directory_or_as_asked(tmp_path, capsys, monkeypatch):
    repo = git_repo(tmp_path / "My_Tool", "main", "git@github.com:acme/my-tool.git", origin_head="main")
    monkeypatch.chdir(repo)
    code, out, _ = init(capsys)  # the cwd
    assert code == 0 and "for project my-tool, with 1 repo(s)" in out
    manifest = load_yaml(repo / "harness.yaml")
    assert (manifest["name"], manifest["hub"]) == ("my-tool", {"project": "my-tool"})
    assert manifest["repos"] == [
        {"name": "My_Tool", "path": "My_Tool", "origin": "git@github.com:acme/my-tool.git", "default_branch": "main"}
    ]
    # the harness is the repo: the harness source reads the harness's own files, the git source the rest
    sources = load_yaml(repo / "knowledge.yaml")["sources"]
    assert sources[0] == {
        "id": "harness",
        "connector": "harness",
        "include": ["harness.yaml", "knowledge.yaml", "contracts.yaml", "plans/*"],
        "assets": False,
        "label": LABEL,
    }
    assert sources[1:] == [
        {"id": "my_tool", "connector": "git", "repo": "My_Tool", "ref": "origin/main", "fetch": True, "label": LABEL}
    ]
    no_issues(repo)
    found = registration(repo)
    assert found.project == "my-tool" and [r["name"] for r in found.body["repos"]] == ["My_Tool"]

    other = git_repo(tmp_path / "other", "main")
    code, out, _ = init(capsys, str(other), "--project", "team-site")
    assert code == 0 and "for project team-site" in out
    assert load_yaml(other / "knowledge.yaml")["project"] == "team-site"
    assert load_yaml(other / "harness.yaml")["hub"] == {"project": "team-site"}
    no_issues(other)


@pytest.mark.parametrize(
    "there, named",
    [
        (["harness.yaml"], "harness.yaml is in"),
        (["knowledge.yaml"], "knowledge.yaml is in"),
        (["harness.yaml", "knowledge.yaml"], "harness.yaml and knowledge.yaml are in"),
    ],
)
def test_harness_init_overwrites_nothing_and_names_the_files_there(tmp_path, capsys, there, named):
    repo = git_repo(tmp_path / "app", "main")
    for name in there:
        (repo / name).write_text(f"# mine: {name}\n", encoding="utf-8")
    code, out, err = init(capsys, str(repo))
    assert (code, out) == (1, "")
    assert named in err and "overwrites nothing" in err
    for name in ("harness.yaml", "knowledge.yaml"):
        path = repo / name
        assert (path.read_text(encoding="utf-8") == f"# mine: {name}\n") if name in there else not path.exists()


def test_harness_init_refuses_a_directory_without_a_repo_or_a_project_name(tmp_path, capsys):
    empty = tmp_path / "plain"
    (empty / "docs").mkdir(parents=True)
    code, _, err = init(capsys, str(empty))
    assert code == 1 and "no git repo in" in err
    assert sorted(p.name for p in empty.iterdir()) == ["docs"]

    repo = git_repo(tmp_path / "app", "main")
    for bad in ("App", "-app", "app_1", "a" * 101):
        code, _, err = init(capsys, str(repo), f"--project={bad}")
        assert code == 2 and "is not a hub project name" in err
    unnamed = git_repo(tmp_path / "___", "main")
    code, _, err = init(capsys, str(unnamed))
    assert code == 2 and "give one with --project NAME" in err
    code, _, err = init(capsys, str(tmp_path / "missing"))
    assert code == 1 and "is not a directory" in err
    assert not any((d / "harness.yaml").exists() for d in (repo, unnamed))


@pytest.mark.parametrize(
    "directory, project",
    [
        ("evo-agents", "evo-agents"),
        ("My_Tool", "my-tool"),
        ("Café Lab 2", "cafe-lab-2"),
        ("--x--", "x"),
        ("___", None),
        ("a" * 120, "a" * 100),
    ],
)
def test_harness_init_makes_a_hub_project_name_of_a_directory_name(directory, project):
    assert project_name(directory) == project


@pytest.mark.parametrize(
    "origin, kept",
    [
        ("https://github.com/acme/api.git", "https://github.com/acme/api.git"),
        ("https://x-access-token:ghs_abc@github.com/acme/api.git", "https://github.com/acme/api.git"),
        ("http://user@git.example.org:8080/a/b", "http://git.example.org:8080/a/b"),
        ("git@github.com:acme/api.git", "git@github.com:acme/api.git"),
        ("ssh://git@github.com/acme/api.git", "ssh://git@github.com/acme/api.git"),
    ],
)
def test_harness_init_keeps_no_password_of_an_origin(origin, kept):
    assert without_secret(origin) == kept


def test_harness_init_gives_each_repo_its_own_source_id(tmp_path):
    root = tmp_path / "ws"
    repos = [Repo(name, root / name, None, "main", False) for name in ("harness", "Harness", ".dots", "a b")]
    ids = [source["id"] for source in knowledge(root, "ws", repos)["sources"]]
    assert ids == ["harness", "harness-2", "harness-3", "dots", "a-b"]
    assert LEVELS == DEFAULT_LEVELS  # the ladder init writes out is the one a knowledge.yaml without one gets
