"""The directory of a run over several repos, with a worktree of each: a plan run, a review run, a judge run and an
author run.

The directory is ``~/.evo/worker/worktrees/<project>-<run>``, the agent's working directory, each repo's worktree in
a folder named as the repo (``gitops.folder_name``). A review run and an author run read only: their worktrees are
detached at the commit origin's default branch has (``RunDirectory.prepare_detached``), so nothing is committed or
pushed (``gitops.check_push`` refuses both kinds). Once the run is over, each worktree on its plan's branch leaves it,
so the owner can check it out elsewhere; a parked run keeps its worktrees as they are, for the run that resumes it.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker import gitops
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.runner import origin
from evo_agents.worker.runner.common import RunFailed

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class RunDirectory:
    """The run's directory (``path``) and the worktree of each of its repos in it (``workspaces``, by repo)."""

    def __init__(self, run: Run):
        self.run = run
        self.path: Path | None = None
        self.workspaces: dict[str, gitops.Workspace] = {}
        run.record.update({"dir": None, "repos": []})

    def save(self) -> None:
        """Note the directory and its worktrees in the run's record."""
        run = self.run
        run.record["dir"] = str(self.path) if self.path is not None else None
        run.record["repos"] = [workspace.to_record() for workspace in self.workspaces.values()]
        run.daemon.home.save_run(run.record)

    def claim(self, again: str) -> Path:
        """The run's own directory under the worker's worktrees; RunFailed when it is there already, saying to remove
        it and ``again``."""
        run = self.run
        self.path = run.daemon.home.worktree_path(run.project, run.id)
        if self.path.exists():
            raise RunFailed(f"{self.path} exists already; remove it and {again}")
        return self.path

    def enter(self) -> None:
        """Make the directory, and make it the agent's."""
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run.worktree = self.path

    def moved(self) -> list[str]:
        """``- <repo>: <folder>/`` for each worktree whose folder is not named as its repo."""
        return [f"- {name}: {ws.worktree.name}/" for name, ws in self.workspaces.items() if ws.worktree.name != name]

    # The review's worktrees, which an author run reads too

    async def prepare_detached(self, repos: list[dict]) -> None:
        """The run's directory, with a worktree of each repo detached at origin's default branch (else the default
        branch the hub names, else the checkout's HEAD), and .evo-run/ for the agent's result."""
        run = self.run
        self.claim("queue the review again")
        self.enter()
        taken: set[str] = set()
        for entry in repos:
            name = entry.get("repo")
            if not isinstance(name, str) or not name:
                raise RunFailed("the review run names a repo without a name")
            checkout = run.daemon.checkout_for(run.project, name)
            if checkout is None:
                raise RunFailed(f"this worker has no checkout of {run.project}/{name}")
            folder = gitops.folder_name(name, taken)
            taken.add(folder)
            self.workspaces[name] = await self.detached(name, checkout, folder)
            self.save()
        (self.path / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        self.save()

    async def detached(self, name: str, checkout: Path, folder: str) -> gitops.Workspace:
        """A worktree of ``name`` at ``<directory>/<folder>``, detached at the commit origin's default branch has."""
        run = self.run
        path = self.path / folder
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from")
            await origin.fetch(run, checkout)
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(run.daemon.config, run.project, name)
            refs = [f"refs/remotes/origin/{branch}" for branch in (remote_head, hub_default) if branch]
            start, base = await origin.start_point(checkout, (*refs, "refs/remotes/origin/HEAD", "HEAD"))
            if base is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to read")
            try:
                await gitops.add_detached_worktree(checkout, path, base)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        run.note(f"Worktree {path} of {name}, detached at {base[:12]} ({start}), read-only.", worktree=str(path))
        return gitops.Workspace(
            repo=name,
            branch=remote_head or hub_default or "HEAD",
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch="HEAD",
            base=base,
            protected=(),
        )

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return [(name, workspace.worktree, workspace.base) for name, workspace in self.workspaces.items()]

    async def diff(self) -> bytes:
        """The diffs of every repo of the run, each under its folder (a/<repo>/...)."""
        parts = []
        for workspace in self.workspaces.values():
            if workspace.worktree.is_dir():
                with contextlib.suppress(gitops.GitError, OSError, asyncio.TimeoutError):
                    parts.append(await gitops.diff(workspace.worktree, workspace.base, prefix=workspace.worktree.name))
        return b"".join(parts)

    async def release(self) -> None:
        """Leave each plan branch once the run is over, so it can be checked out elsewhere; a parked run keeps its
        worktrees as they are, for the run that resumes it."""
        if self.run.parked:
            return
        for workspace in self.workspaces.values():
            if workspace.local_branch == workspace.branch and workspace.worktree.exists():
                with contextlib.suppress(gitops.GitError, OSError):
                    await gitops.detach(workspace.worktree)
