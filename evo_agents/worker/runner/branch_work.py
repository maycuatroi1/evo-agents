"""The work of a run on one branch of every repo of its project, which a chat run (``chat``) and a task run (``task``)
share: a writable worktree of each repo its worker has a checkout of, in the run's directory, on the run's branch
(``chat/<id>`` or ``task/<id>``), from origin's branch when it was pushed already, else from origin's default branch,
or the worktrees of the chat run before it in its session, adopted as they are and their checkouts fetched;
and at the run's end, what the agent left committed in each and the run's branch pushed of each repo whose commits the
branch lacks on origin, never a default branch and never forced (``gitops.check_push``, by the run's kind), with the
hub told of each push (POST /v1/worker/runs/{id}/<kind>/pulls), which opens the draft pull request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker import gitops
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner import origin, parallel, push
from evo_agents.worker.runner.common import HARD_STOP_TRIES, RunFailed, push_cause, wait_or

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run
    from evo_agents.worker.runner.directory import RunDirectory

NOTHING = {"files": 0, "insertions": 0, "deletions": 0}


@dataclass(frozen=True)
class Kind:
    """What tells a kind's branch work apart: the run's kind, which ``gitops.check_push`` checks the push by; the
    route the hub hears of a push on; where the hub links it, in the notes; and how a run of the kind runs again."""

    name: str
    route: str
    linked: str
    again: str


CHAT = Kind("chat", "chat/pulls", "in the thread", "mention the agent again")


class BranchWork:
    """The worktrees of a run on its ``branch`` in its ``directory``, and their push at its end, for a run of
    ``kind``."""

    def __init__(self, run: Run, directory: RunDirectory, branch: str, kind: Kind):
        self.run = run
        self.directory = directory
        self.branch = branch
        self.kind = kind
        self.left_out: list[str] = []  # repos of the run this worker has no checkout of

    def held_repos(self, repos: list[dict]) -> list[dict]:
        """The repos of the run this worker has a checkout of; the others are noted, and left out."""
        run = self.run
        held = []
        for entry in repos:
            name = entry.get("repo")
            if isinstance(name, str) and name and run.daemon.checkout_for(run.project, name) is not None:
                held.append(entry)
            else:
                self.left_out.append(str(name))
        if self.left_out:
            run.note(f"No checkout of {', '.join(self.left_out)} on this worker: the agent works without them.")
        return held

    def where(self) -> list[str]:
        """What the prompt adds: where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        moved = self.directory.moved()
        extra = []
        if moved:
            extra += ["", "Worktree folders that are not named as their repo:", *moved]
        if self.left_out:
            extra += ["", f"Repos of the project this worker has no checkout of: {', '.join(self.left_out)}."]
        return extra

    def adopted(self, names: list[str]) -> list[str]:
        """The repos of ``names`` whose worktree the run goes on in, adopted from the run before it in its session."""
        return [name for name in names if name in self.directory.workspaces]

    async def prepare(self, held: list[dict]) -> None:
        """The run's directory with a writable worktree of each repo of ``held`` on the run's branch: the earlier
        run's, adopted, their checkouts fetched several at a time (``fetch_adopted``), else new ones, made several at
        a time (``RunDirectory.make_each``)."""
        run, directory = self.run, self.directory
        if directory.path is None:
            directory.claim(self.kind.again)
        directory.enter()
        adopted = list(directory.workspaces.values())
        if len(adopted) > 1:
            at_once = min(parallel.GIT_AT_ONCE, len(adopted))
            run.note(f"Fetching origin of the {len(adopted)} repos the run goes on in, {at_once} at a time.")
        await parallel.each(run, adopted, self.fetch_adopted)
        await directory.make_each(
            [entry["repo"] for entry in held],
            lambda name, folder: self.make_worktree(name, run.daemon.checkout_for(run.project, name), folder),
        )
        (directory.path / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        directory.save()

    async def fetch_adopted(self, workspace: gitops.Workspace) -> None:
        """Fetch origin in the checkout of a worktree the run goes on in, under the checkout's lock, with the run's
        leases: the check that git reads it, which the preflight left to it; RunFailed with cause credentials or
        checkout when git cannot (``origin.fetch``). The worktree stays as the run before left it."""
        async with self.run.daemon.repo_lock(workspace.checkout):
            await origin.fetch(self.run, workspace.checkout, repo=workspace.repo)

    async def make_worktree(self, name: str, checkout: Path, folder: str) -> gitops.Workspace:
        """A worktree of ``name`` at ``<directory>/<folder>`` on the run's branch: from origin's branch when the
        run's branch was pushed already, else from origin's default branch; on evo-run/<run>/<folder> when the branch
        is checked out elsewhere or has commits here that the start lacks."""
        run, branch = self.run, self.branch
        path = self.directory.path / folder
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await origin.fetch(run, checkout, repo=name)
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(run.daemon.config, run.project, name)
            protected = tuple(sorted(origin.protected_branches(remote_head, hub_default)))
            defaults = [f"refs/remotes/origin/{default}" for default in (remote_head, hub_default) if default]
            refs = (f"refs/remotes/origin/{branch}", *defaults, "refs/remotes/origin/HEAD", "HEAD")
            start, base = await origin.start_point(checkout, refs)
            if base is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to start from")
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and {self.kind.again}")
            side = origin.RUN_REPO_BRANCH.format(id=run.id, folder=folder)
            local_branch, why = await origin.side_or_branch(checkout, branch, base, side)
            if why is not None:
                run.note(f"{branch} of {name} {why}; the run works on {local_branch} and pushes it to {branch}.")
            try:
                await gitops.add_worktree(checkout, path, local_branch, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        run.note(
            f"Worktree {path} of {name} on {local_branch} at {base[:12]} ({start}).", worktree=str(path), repo=name
        )
        return gitops.Workspace(
            repo=name,
            branch=branch,
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch=local_branch,
            base=base,
            protected=protected,
        )

    async def push_repos(self) -> dict:
        """Commit what the agent left in each repo and push the run's branch of each repo it changed; the diffstat of
        the whole run."""
        total = dict(NOTHING)
        for name, workspace in self.directory.workspaces.items():
            stat = await self.push_repo(name, workspace)
            for key in total:
                total[key] += stat[key]
        return total

    async def push_repo(self, name: str, workspace: gitops.Workspace) -> dict:
        """Commit what the agent left in the worktree of ``name`` and push the run's branch when it has commits the
        worktree did not start with; the repo's diffstat. The hub hears of each push that moved the branch."""
        run, path = self.run, workspace.worktree
        if not path.is_dir():
            raise RunFailed(f"the worktree of {name} at {path} is gone")
        try:
            await push.commit(run, path, name)
        except gitops.GitError as exc:
            raise RunFailed(f"committing what the agent left in {name} failed: {exc}") from None
        if await gitops.rev(path, "HEAD") == workspace.base:
            return dict(NOTHING)  # the agent changed nothing in this repo
        head_branch = await gitops.current_branch(path)
        if head_branch != workspace.local_branch:
            raise RunFailed(
                f"the worktree of {name} is on "
                f"{gitops.branch_ref(head_branch) if head_branch else 'a detached HEAD'}, not "
                f"{gitops.branch_ref(workspace.local_branch)}: the agent switched branches, and the worker does not "
                "push it",
                cause="branch_mismatch",
            )
        run.check()

        async def pushed_branch() -> gitops.Pushed:
            with run.credentials.ticketed(run.daemon.env) as env:
                return await gitops.push(
                    path,
                    workspace.branch,
                    protected=workspace.protected,
                    kind=self.kind.name,
                    plan_branch=None,
                    env=env,
                )

        try:
            pushed = await run.credentials.with_renewal(name, pushed_branch)
        except gitops.PushRefused as exc:
            raise RunFailed(f"{name}: {exc}") from None
        except gitops.GitError as exc:
            raise RunFailed(f"git push of {name} to {workspace.branch} failed: {exc}", cause=push_cause(exc)) from None
        if pushed.changed:
            run.note(
                f"Pushed {pushed.head[:12]} of {name} to {workspace.branch} on origin "
                f"({len(pushed.commits)} commit(s)).",
                commit_sha=pushed.head,
                repo=name,
            )
            await self.tell_hub(name, workspace.branch, pushed.head)
        return await gitops.diffstat(path, workspace.base)

    async def tell_hub(self, name: str, branch: str, head: str) -> None:
        """Tell the hub the run pushed ``branch`` of ``name``, for its draft pull request, trying a few times; what
        the hub does not take is noted, and the run goes on."""
        run, linked = self.run, self.kind.linked
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                answer = await run.daemon.hub.in_chats(
                    run.id, self.kind.route, {"repo": name, "branch": branch, "head": head}
                )
                run.note(f"The hub linked {branch} of {name} {linked}: {answer.get('url') or 'no pull request'}.")
                return
            except Unreachable:
                await wait_or(run.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                run.note(f"The hub did not take the push of {branch} of {name}: {exc}")
                return
        run.note(f"The hub did not answer: the push of {branch} of {name} was not linked {linked}.")
