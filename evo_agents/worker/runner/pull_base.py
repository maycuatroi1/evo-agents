"""The base of the pull request of a run's branch (``BranchWork.push_repo``): the branch of origin the branch was made
from, which the worker names when it tells the hub of a push (POST .../chat/pulls and .../task/pulls, ``base``), so
the hub opens the draft pull request into it rather than into the repo's default branch.

A chat run's worktree starts from origin's default branch, but its agent may move the branch onto another one (a
``git reset --hard origin/deploy/test``, a rebase, a cherry-pick of a branch's commits), so the start is no base. The
base is the branch of origin that lacks the fewest commits of HEAD, the run's branch and the hub's own branches
(``chat/``, ``task/``, ``evo-run/``) left out: the branch HEAD was made from lacks only the commits made since, and a
branch HEAD is on lacks none, which the hub then reads as nothing to open. Ties go, in order, to the branches the
worktree's reflog names (``origin/<branch>`` in a checkout, reset, rebase or pull the agent made, newest first), the
default branch the hub lists, and origin's default branch; then to the branch with fewer commits that HEAD lacks; then
by name. A base the run's task names is the base whatever the counts say.

Git counts them in one pass (``for-each-ref`` with ``%(ahead-behind:HEAD)``, git 2.41); an older git counts each of at
most FALLBACK_BRANCHES branches with ``rev-list``. A worktree git cannot read gives no base, and the hub picks one.
"""

from __future__ import annotations

import re
from pathlib import Path

from evo_agents.worker import gitops

HUB_BRANCHES = ("chat/", "task/", "evo-run/")  # the hub's and the worker's own branches, never a base
REFLOG_ENTRIES = 200  # the newest entries of each reflog read
FALLBACK_BRANCHES = 300  # branches an older git counts one by one
_ORIGIN_REF = re.compile(r"\borigin/([A-Za-z0-9._/-]+)")


def _candidate(name: str, own: str) -> bool:
    return bool(name) and name not in ("HEAD", own) and not name.startswith(HUB_BRANCHES)


async def counts(worktree: Path, own: str) -> dict[str, tuple[int, int]]:
    """{branch of origin: (the commits of HEAD it lacks, the commits it has that HEAD lacks)}, the run's branch
    ``own`` and the hub's branches left out; empty when git cannot say."""
    form = "--format=%(refname:lstrip=3)%00%(ahead-behind:HEAD)"
    code, out, _ = await gitops.git(worktree, "for-each-ref", form, "refs/remotes/origin/", check=False)
    found: dict[str, tuple[int, int]] = {}
    if code == 0:
        for line in out.splitlines():
            name, _, numbers = line.partition("\0")
            parts = numbers.split()
            if _candidate(name, own) and len(parts) == 2 and all(part.isdigit() for part in parts):
                found[name] = (int(parts[1]), int(parts[0]))  # ahead-behind of the ref: (it has, HEAD has)
        return found
    code, out, _ = await gitops.git(worktree, "for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin/")
    names = [name for name in out.splitlines() if _candidate(name, own)][:FALLBACK_BRANCHES]
    for name in names:
        code, out, _ = await gitops.git(
            worktree, "rev-list", "--left-right", "--count", f"refs/remotes/origin/{name}...HEAD", check=False
        )
        parts = out.split()
        if code == 0 and len(parts) == 2 and all(part.isdigit() for part in parts):
            found[name] = (int(parts[1]), int(parts[0]))
    return found


async def reflog_names(worktree: Path, local_branch: str) -> list[str]:
    """The branches of origin the reflogs of the worktree's HEAD and of ``local_branch`` name, newest first, each
    once."""
    names: list[str] = []
    for ref in ("HEAD", f"refs/heads/{local_branch}"):
        code, out, _ = await gitops.git(
            worktree, "reflog", "show", "--format=%gs", f"-n{REFLOG_ENTRIES}", ref, "--", check=False
        )
        if code != 0:
            continue
        for line in out.splitlines():
            names += [match.rstrip("/.") for match in _ORIGIN_REF.findall(line)]
    return list(dict.fromkeys(name for name in names if name))


async def pick(
    worktree: Path,
    *,
    own: str,
    local_branch: str,
    defaults: tuple[str | None, ...] = (),
    named: str | None = None,
) -> str | None:
    """The base of the pull request of the run's branch ``own`` from the worktree at ``worktree`` on
    ``local_branch``: ``named`` (the base the run's task names) when given, else the branch of origin that lacks the
    fewest commits of HEAD, ties broken as the module says, ``defaults`` being the hub's default branch and origin's;
    None when git names no branch."""
    if named:
        return named
    found = await counts(worktree, own)
    if not found:
        return None
    preferred = [*await reflog_names(worktree, local_branch), *(name for name in defaults if name)]
    rank: dict[str, int] = {}
    for index, name in enumerate(preferred):
        rank.setdefault(name, index)
    last = len(preferred)
    return min(found, key=lambda name: (found[name][0], rank.get(name, last), found[name][1], name))
