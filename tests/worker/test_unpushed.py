"""A worktree left on a detached HEAD at a commit origin has (a review of a PR) holds nothing to push."""

import asyncio
import os
import subprocess
from pathlib import Path

from evo_agents.worker import gitops

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **IDENTITY}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout


def test_a_detached_head_at_a_commit_of_origin_is_not_unpushed_and_a_new_commit_is(tmp_path):
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    git(tmp_path, "init", "-q", "-b", "main", str(origin))
    git(origin, "commit", "-q", "--allow-empty", "-m", "base")
    git(origin, "checkout", "-q", "-b", "pr")
    git(origin, "commit", "-q", "--allow-empty", "-m", "pr work")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "--detach", "origin/pr")
    assert not asyncio.run(gitops.unpushed(clone))
    git(clone, "commit", "-q", "--allow-empty", "-m", "the agent's own")
    assert asyncio.run(gitops.unpushed(clone))
