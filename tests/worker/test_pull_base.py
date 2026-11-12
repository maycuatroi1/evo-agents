"""The base of a run branch's pull request (``evo_agents.worker.runner.pull_base``) and what a run's commit leaves out
(``gitops.commit_run``, ``gitops.ignore_worker_files``), on real git repos.

The base is the branch of origin that lacks the fewest commits of HEAD, the hub's branches left out: a branch reset
onto origin/deploy/test gets deploy/test, not main; a branch whose head is another branch's gets that branch (lacking
nothing, which the hub reads as nothing to open); a tie goes to what the reflog names; a base the task names wins. A
``.venv`` link the agent made is no part of the run's commit, and the repository ignores it once a worktree is made.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from evo_agents.worker import gitops
from evo_agents.worker.runner import pull_base

IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(cwd: Path, name: str, text: str = "x\n") -> str:
    (cwd / name).parent.mkdir(parents=True, exist_ok=True)
    (cwd / name).write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "--quiet", "-m", f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    """A clone of an origin with main, deploy/test two commits ahead of it, deploy/evobright at deploy/test's first
    commit, qa/fix-18 one commit on deploy/test, and an old chat/7; the clone on chat/1 from origin/main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "--initial-branch", "main")
    commit(seed, "README.md")
    git(seed, "checkout", "--quiet", "-b", "deploy/test")
    commit(seed, "a.py")
    git(seed, "branch", "deploy/evobright")
    commit(seed, "b.py")
    git(seed, "checkout", "--quiet", "-b", "qa/fix-18")
    commit(seed, "c.py")
    git(seed, "checkout", "--quiet", "-b", "chat/7", "deploy/test")
    commit(seed, "d.py")
    git(seed, "checkout", "--quiet", "main")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    git(clone, "checkout", "--quiet", "-b", "chat/1", "origin/main")
    # gitops.commit_run commits as the machine's git identity, which a CI runner has none of (release CI of v0.64.0)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        git(clone, "config", key, value)
    return clone


def pick(clone: Path, **kwargs) -> str | None:
    defaults = kwargs.pop("defaults", ("develop", "main"))
    return asyncio.run(pull_base.pick(clone, own="chat/1", local_branch="chat/1", defaults=defaults, **kwargs))


def test_a_branch_reset_onto_another_branch_gets_it_as_its_base(repo):
    assert pick(repo) == "main", "from main and no commit yet: main"
    git(repo, "reset", "--quiet", "--hard", "origin/deploy/test")
    commit(repo, "fix.py")
    assert pick(repo) == "deploy/test", "deploy/evobright lacks one commit more"
    found = asyncio.run(pull_base.counts(repo, "chat/1"))
    assert found["deploy/test"] == (1, 0) and found["main"] == (3, 0) and "chat/7" not in found


def test_a_branch_on_another_branchs_head_gets_that_branch(repo):
    git(repo, "reset", "--quiet", "--hard", "origin/qa/fix-18")
    assert pick(repo) == "qa/fix-18", "it lacks nothing of HEAD: the hub opens nothing"


def test_a_tie_goes_to_the_branch_the_reflog_names_then_the_defaults(repo, tmp_path):
    git(repo, "push", "--quiet", "origin", "origin/deploy/test:refs/heads/release")  # another branch at the same commit
    git(repo, "fetch", "--quiet", "origin")
    git(repo, "reset", "--quiet", "--hard", "origin/release")
    commit(repo, "fix.py")
    assert pick(repo) == "release", "the reflog names origin/release"
    git(repo, "reflog", "expire", "--expire=now", "--all")
    assert pick(repo, defaults=("deploy/test",)) == "deploy/test", "no reflog: the default the hub lists"


def test_the_base_the_task_names_wins(repo):
    git(repo, "reset", "--quiet", "--hard", "origin/deploy/test")
    assert pick(repo, named="main") == "main"


def test_a_venv_link_is_left_out_of_the_runs_commit_and_ignored_by_the_repository(repo, tmp_path):
    cache = tmp_path / "cache" / "venv313"
    cache.mkdir(parents=True)
    (repo / ".venv").symlink_to(cache)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    made = asyncio.run(gitops.commit_run(repo, "run #1: fix"))
    assert made.made and made.left_out == (".venv",)
    assert git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == ["app.py"]
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? .venv"
    # a worktree made of the checkout: its repository ignores .venv, once
    worktree = tmp_path / "wt"
    asyncio.run(gitops.add_worktree(repo, worktree, "chat/2", "HEAD", reset=True))
    asyncio.run(gitops.add_worktree(repo, tmp_path / "wt3", "chat/3", "HEAD", reset=True))
    exclude = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").count(gitops.EXCLUDE_MARK) == 1
    (worktree / ".venv").symlink_to(cache)
    assert git(worktree, "status", "--porcelain") == "", "ignored in the worktree too"
    assert git(repo, "status", "--porcelain") == "", "and in the checkout"


def test_a_venv_the_repo_tracks_stays_the_repos(repo):
    commit(repo, ".venv", "name = project\n")  # a file some tools read, tracked on purpose
    (repo / ".venv").write_text("name = other\n", encoding="utf-8")
    made = asyncio.run(gitops.commit_run(repo, "run #1: rename"))
    assert made.made and made.left_out == ()
