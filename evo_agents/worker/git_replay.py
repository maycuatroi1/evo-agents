"""The replay of a run's own commits on top of a branch the remote moved on, for ``gitops.push`` with a ``Replay``
(the module docstring of ``gitops`` tells when it runs and what it never does).

The commits are replayed with ``git rebase`` in a worktree of their own (``replay_commits``), the run's worktree moves
to the result (``reset --keep``, so changes it has that the remote's commits do not touch stay), the verify the replay
names runs again there, and the result is pushed as a fast-forward; a remote that moved on again before the push lands
gets the same, REPLAY_TRIES times in all. When the commits do not go on top, HEAD goes to the replay's side branch and
``push_replaying`` raises ``PushConflict``.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Collection, Mapping
from pathlib import Path

from evo_agents.hub.runs import MAX_NOTICE_COMMITS
from evo_agents.worker.gitops import (
    GitAuthError,
    GitError,
    PushConflict,
    Pushed,
    Replay,
    git,
    has_commit,
    is_ancestor,
    new_commits,
    prune_worktrees,
    rejected,
    remote_tip,
    rev,
    send,
    tail,
    without_hooks,
)

REPLAY_TRIES = 3  # replays of a run's commits onto a default branch the remote keeps moving on before the push lands
MAX_CONFLICT_PATHS = 10  # paths of a replay's conflict its message names


class Unreplayable(GitError):
    """The run's commits did not replay on top of the remote's tip; the message says why (the paths of a conflict)."""


async def push_replaying(
    cwd: Path,
    branch: str,
    remote: str,
    head: str,
    replay: Replay,
    default: bool,
    *,
    env: Mapping[str, str] | None,
    options: Collection[str],
) -> Pushed:
    """``push`` of a default branch the plan names, with ``replay``: HEAD as a fast-forward, after its own commits were
    replayed on top of the remote's tip when the remote moved the branch on, as many as REPLAY_TRIES times."""
    original, onto = head, None
    for attempt in range(1, REPLAY_TRIES + 1):
        before = await remote_tip(cwd, branch, remote, env=env)
        if before == head or (before is not None and await has_commit(cwd, before, head, branch, remote, env=env)):
            return Pushed(branch, head, default, False, (), onto, original if head != original else None)
        if before is not None and not await is_ancestor(cwd, before, head):
            if await rev(cwd, before) is None:  # has_commit fetched it; nothing replays onto a commit not here
                raise GitError(f"the tip {before[:12]} of {branch} on {remote} could not be fetched to replay onto")
            head = await _replay(cwd, branch, remote, before, head, original, replay, env=env)
            onto = before
            if head == before:  # every commit of the run was on the remote's branch already, as another commit
                return Pushed(branch, head, default, False, (), onto, original)
        commits = tuple(await new_commits(cwd, before, remote))
        try:
            await send(cwd, "HEAD", branch, remote, env=env, options=options)
        except GitError as exc:
            if not rejected(exc) or isinstance(exc, GitAuthError):
                raise
            if attempt == REPLAY_TRIES:
                why = f"went on top of it {REPLAY_TRIES} times, and each time it moved on again before the push"
                raise await _conflict(cwd, branch, remote, original, before or head, why, replay, env=env) from None
            continue  # the remote moved the branch on between the look and the push
        return Pushed(branch, head, default, True, commits, onto, original if head != original else None)
    raise AssertionError("unreachable")  # pragma: no cover


async def _replay(
    cwd: Path,
    branch: str,
    remote: str,
    onto: str,
    head: str,
    original: str,
    replay: Replay,
    *,
    env: Mapping[str, str] | None,
) -> str:
    """The commits of ``head`` that ``onto`` lacks, replayed on top of ``onto``, the worktree at ``cwd`` moved there and
    the replay's verify run again in it; the new HEAD. PushConflict, the worktree back at ``original`` and that on the
    side branch, when they do not go on top or their verify fails."""

    async def conflict(why: str) -> PushConflict:
        return await _conflict(cwd, branch, remote, original, onto, why, replay, env=env)

    for sha in replay.pushed:
        if sha and await is_ancestor(cwd, sha, head) and not await is_ancestor(cwd, sha, onto):
            raise await conflict(
                f"include {sha[:12]}, which the run pushed to {branch} before and the remote no longer has: the worker "
                "does not put back a commit the remote dropped"
            )
    _, merges, _ = await git(cwd, "rev-list", "--merges", f"{onto}..{head}", check=False)
    if merges.split():
        raise await conflict(f"hold a merge commit ({merges.split()[0][:12]}), which the worker does not replay")
    try:
        new = await replay_commits(cwd, onto, head, replay.scratch)
    except Unreplayable as exc:
        raise await conflict(f"did not go on top of it ({exc})") from None
    code, out, err = await git(cwd, "reset", "--quiet", "--keep", new, check=False)
    if code != 0:
        why = f"went on top of it, but the worktree has changes the remote's commits touch ({tail(err or out, 300)})"
        raise await conflict(why)
    if replay.verify is not None:
        failed = await replay.verify(new)
        if failed:
            raise await conflict(f"went on top of it, but the verify run again there failed: {failed}")
    return new


async def replay_commits(cwd: Path, onto: str, head: str, scratch: Path) -> str:
    """The commits of ``head`` that ``onto`` lacks, replayed on top of ``onto`` with ``git rebase`` in a worktree of
    their own at ``scratch``, detached and without the repository's hooks, which is removed after: the commit the
    replay ends on. A commit whose change ``onto`` has already is dropped. Unreplayable when one does not apply, naming
    the paths in conflict; nothing of the repository's branches moves either way."""
    local = {**without_hooks(None), "GIT_EDITOR": "true"}
    await _drop_scratch(cwd, scratch)
    scratch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        await git(cwd, "worktree", "add", "--quiet", "--detach", str(scratch), head, env=local)
        code, out, err = await git(
            scratch,
            "-c",
            "rebase.updateRefs=false",
            "-c",
            "rebase.autoStash=false",
            "rebase",
            "--quiet",
            "--no-autosquash",
            onto,
            check=False,
            env=local,
        )
        if code != 0:
            _, unmerged, _ = await git(scratch, "diff", "--name-only", "--diff-filter=U", check=False, env=local)
            await git(scratch, "rebase", "--abort", check=False, env=local)
            paths = unmerged.split()
            if paths:
                more = f" and {len(paths) - MAX_CONFLICT_PATHS} more" if len(paths) > MAX_CONFLICT_PATHS else ""
                raise Unreplayable(f"conflict in {', '.join(paths[:MAX_CONFLICT_PATHS])}{more}")
            raise Unreplayable(tail(err or out, 300) or f"git rebase exited {code}")
        new = await rev(scratch, "HEAD")
        if new is None:
            raise Unreplayable("the replay ended on no commit")
        return new
    finally:
        await _drop_scratch(cwd, scratch)


async def _drop_scratch(cwd: Path, scratch: Path) -> None:
    await git(cwd, "worktree", "remove", "--force", str(scratch), check=False)
    if scratch.exists():
        await asyncio.to_thread(shutil.rmtree, scratch, True)
    await prune_worktrees(cwd)


async def _conflict(
    cwd: Path,
    branch: str,
    remote: str,
    original: str,
    onto: str,
    why: str,
    replay: Replay,
    *,
    env: Mapping[str, str] | None,
) -> PushConflict:
    """Put the worktree back at ``original`` and push it to the replay's side branch, never forced (a side branch
    that has it already is left alone); the PushConflict that says so. A push to the side branch that fails raises.
    When ``reset --keep`` will not take the worktree back, because the verify run again changed a file the remote's
    commits brought, the worktree stays at the replayed commit with that change, and the message says so."""
    stays = ""
    if await rev(cwd, "HEAD") != original:
        code, out, err = await git(cwd, "reset", "--quiet", "--keep", original, check=False)
        head = await rev(cwd, "HEAD")
        if code != 0 and head is not None and head != original:
            stays = (
                f"; the worktree stays at {head[:12]}, the commits on top of {branch}, since going back would "
                f"overwrite changes made there ({tail(err or out, 300)})"
            )
    side = replay.side_branch
    tip = await remote_tip(cwd, side, remote, env=env)
    if tip != original and not (tip is not None and await has_commit(cwd, tip, original, side, remote, env=env)):
        await send(cwd, original, side, remote, env=env)
    _, out, _ = await git(cwd, "rev-list", f"--max-count={MAX_NOTICE_COMMITS}", original, f"^{onto}", check=False)
    commits = tuple(out.split())
    message = (
        f"{branch} of {remote} moved on to {onto[:12]}, and the run's commits {why}. They are on {side} of {remote} "
        f"now, at {original[:12]} ({len(commits)} commit(s) {branch} lacks); {branch} was not touched{stays}"
    )
    return PushConflict(message, branch=branch, side_branch=side, head=original, onto=onto, commits=commits)
