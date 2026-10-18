"""The worker compares the branch of a worktree by its full ref (``refs/heads/<name>``), so a tag with the branch's
name, which makes ``git symbolic-ref --short`` print ``heads/<name>``, is no mismatch (run 161)."""

import asyncio
import os
import subprocess
from pathlib import Path

from evo_agents.worker import gitops

IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env={**os.environ, **IDENTITY}
    )
    return done.stdout.strip()


def repo_with_tag_named_like_its_branch(tmp_path: Path, name: str) -> Path:
    git(tmp_path, "init", "--quiet", "-b", "main")
    git(tmp_path, "commit", "--quiet", "--allow-empty", "-m", "first")
    git(tmp_path, "tag", name)
    git(tmp_path, "checkout", "--quiet", "-b", name)
    return tmp_path


def test_a_tag_with_the_branch_name_does_not_change_the_branch_or_ref_read(tmp_path):
    name = "feat/queue"
    repo = repo_with_tag_named_like_its_branch(tmp_path, name)
    assert git(repo, "symbolic-ref", "--short", "HEAD") == f"heads/{name}", "the ambiguity this guards against"
    assert asyncio.run(gitops.current_ref(repo)) == f"refs/heads/{name}"
    assert asyncio.run(gitops.current_branch(repo)) == name
    assert gitops.branch_ref(name) == f"refs/heads/{name}" == gitops.branch_ref(f"refs/heads/{name}")


def test_a_detached_head_has_no_branch_or_ref(tmp_path):
    git(tmp_path, "init", "--quiet", "-b", "main")
    git(tmp_path, "commit", "--quiet", "--allow-empty", "-m", "first")
    git(tmp_path, "checkout", "--quiet", "--detach")
    assert asyncio.run(gitops.current_ref(tmp_path)) is None
    assert asyncio.run(gitops.current_branch(tmp_path)) is None
