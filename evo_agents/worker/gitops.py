"""The git commands of a run, run as subprocesses of the daemon's event loop.

Every command runs with ``GIT_TERMINAL_PROMPT=0`` (a push that would ask for a password fails instead of waiting)
and ``LC_ALL=C`` (messages the daemon reads are in English), with a timeout, and fails with ``GitError`` carrying the
end of git's own message. Nothing here forces a push, rewrites a branch that has commits of its own, or merges.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path

from evo_agents.hub.runs import RESULT_DIR

TIMEOUT = 120.0
NETWORK_TIMEOUT = 600.0  # fetch and push
MAX_MESSAGE = 2000
PROTECTED = ("main", "master")  # never pushed, whatever the remote's default branch is
EXCLUDE_RESULT = f":(exclude){RESULT_DIR}"  # the agent's result file stays out of every commit
_SHORTSTAT = re.compile(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")


class GitError(Exception):
    pass


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def _tail(text: str, limit: int = MAX_MESSAGE) -> str:
    text = " ".join(text.strip().split())
    return text if len(text) <= limit else "..." + text[-limit:]


async def git(cwd: Path, *args: str, timeout: float = TIMEOUT, check: bool = True) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of ``git -C cwd ARGS``; GitError on a failure when ``check``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(),
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise GitError(f"git {args[0]} gave no answer within {timeout:g}s") from None
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError(f"git {args[0]} failed ({proc.returncode}): {_tail(stderr or stdout)}")
    return proc.returncode, stdout, stderr


async def rev(cwd: Path, ref: str) -> str | None:
    """The commit ``ref`` names, or None."""
    code, out, _ = await git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.strip() if code == 0 and out.strip() else None


async def has_remote(cwd: Path, name: str = "origin") -> bool:
    code, out, _ = await git(cwd, "remote", check=False)
    return code == 0 and name in out.split()


async def fetch(cwd: Path, remote: str = "origin") -> None:
    await git(cwd, "fetch", "--prune", "--quiet", remote, timeout=NETWORK_TIMEOUT)


async def remote_default_branch(cwd: Path, remote: str = "origin") -> str | None:
    """The branch the remote's HEAD points at, as the last clone or ``remote set-head`` saw it."""
    code, out, _ = await git(cwd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False)
    name = out.strip()
    return name.removeprefix(f"{remote}/") if code == 0 and name.startswith(f"{remote}/") else None


async def branches_in_worktrees(cwd: Path) -> set[str]:
    """The branches checked out in any worktree of the repository."""
    _, out, _ = await git(cwd, "worktree", "list", "--porcelain")
    return {
        line.removeprefix("branch refs/heads/") for line in out.splitlines() if line.startswith("branch refs/heads/")
    }


async def is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    code, _, _ = await git(cwd, "merge-base", "--is-ancestor", older, newer, check=False)
    return code == 0


async def check_branch_name(cwd: Path, branch: str) -> bool:
    code, _, _ = await git(cwd, "check-ref-format", "--branch", branch, check=False)
    return code == 0 and not branch.startswith("-")


async def add_worktree(cwd: Path, path: Path, branch: str, start: str, *, reset: bool) -> None:
    """A worktree at ``path`` on ``branch`` at ``start``; ``reset`` moves an existing branch there (-B)."""
    await git(cwd, "worktree", "add", "--no-track", "-B" if reset else "-b", branch, str(path), start)


async def current_branch(cwd: Path) -> str | None:
    """The branch checked out at ``cwd``; None when HEAD is detached."""
    code, out, _ = await git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return out.strip() if code == 0 and out.strip() else None


async def has_changes(cwd: Path) -> bool:
    """Whether the work tree has changes to commit, the agent's result file aside."""
    _, out, _ = await git(cwd, "status", "--porcelain", "--untracked-files=all", "--", ".", EXCLUDE_RESULT)
    return bool(out.strip())


async def commit_all(cwd: Path, message: str) -> bool:
    """Commit every change but the result file; whether there was one."""
    if not await has_changes(cwd):
        return False
    await git(cwd, "add", "--all", "--", ".", EXCLUDE_RESULT)
    await git(cwd, "commit", "--quiet", "-m", message)
    return True


async def diffstat(cwd: Path, base: str, head: str = "HEAD") -> dict:
    _, out, _ = await git(cwd, "diff", "--shortstat", base, head)
    match = _SHORTSTAT.search(out)
    if not match:
        return {"files": 0, "insertions": 0, "deletions": 0}
    files, insertions, deletions = (int(value or 0) for value in match.groups())
    return {"files": files, "insertions": insertions, "deletions": deletions}


async def diff(cwd: Path, base: str, head: str = "HEAD") -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        "diff",
        "--binary",
        base,
        head,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_env(),
    )
    out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    if proc.returncode != 0:
        raise GitError(f"git diff failed ({proc.returncode})")
    return out


async def push(cwd: Path, branch: str, remote: str = "origin") -> None:
    """Push HEAD to ``branch`` of the remote: a fast-forward or nothing, never forced."""
    await git(cwd, "push", "--quiet", "--porcelain", remote, f"HEAD:refs/heads/{branch}", timeout=NETWORK_TIMEOUT)


async def detach(cwd: Path) -> None:
    """Leave the branch the worktree is on, so the owner can check it out elsewhere; the files stay."""
    await git(cwd, "checkout", "--quiet", "--detach")


async def remove_worktree(cwd: Path, path: Path) -> None:
    await git(cwd, "worktree", "remove", "--force", str(path))


async def prune_worktrees(cwd: Path) -> None:
    await git(cwd, "worktree", "prune", check=False)


async def delete_branch(cwd: Path, branch: str) -> None:
    await git(cwd, "branch", "--quiet", "-D", branch, check=False)
