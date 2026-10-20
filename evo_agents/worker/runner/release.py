"""A release run (kind ``release``, ``ReleaseRun``, ``evo_agents.hub.release_runs``): the cut of a new version, or the
fix forward of a red tag, in a cycle of a project's daily release.

The daemon takes the run's leases for the one repo its claim names (``release.repo``), checks it can read it and push
to it, fetches origin and makes the directory ``~/.evo/worker/worktrees/<project>-<run>`` with a writable worktree of
the repo at the commit the claim names (``release.start_commit``: a cut's target commit), on a branch of the run
(``evo-run/<run>/<folder>``). The agent works in EVO_RUN_KIND ``release``, one headless turn that asks nobody anything:
it picks the version, runs the bump, looks for the faults its prompt names, commits, and writes the version and its
Decision line in ``.evo-run/result.json``. When it ends, the daemon commits what the agent left and pushes HEAD to
``release/<version>`` on origin, never a default branch, never a tag and never forced (``gitops.check_push``, kind
``release``), tells the hub (POST /v1/worker/runs/{id}/release/branch), and reports ``done``; the hub opens the pull
request, merges it and tags the merge commit (``evo_agents.hub.server.release_land``). No version X.Y.Z in the
result file, no commit of the agent, or a hub that does not take the push fails the run, and its cycle stops.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import release_runs, releases, runs
from evo_agents.worker import gitops
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner import access, origin, push, verify
from evo_agents.worker.runner.common import HARD_STOP_TRIES, RunFailed, push_cause, wait_or
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

RESULT_MAX_BYTES = 1024 * 1024
AGAIN = "let the release cycle queue its run again"


async def _no_more_turns() -> list[dict]:
    """A release run is one turn: nobody writes to its agent."""
    return []


class ReleaseRun:
    """A release run: a writable worktree of the schedule's repo at the commit the release starts from, one headless
    turn of the agent, and HEAD pushed to release/<version> at its end, with the hub told so for the landing."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        found = run.spec.get("release")
        self.release = found if isinstance(found, dict) else {}
        run.title = run.spec.get("title") or f"release run of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=_no_more_turns)

    async def steps(self) -> None:
        run, release = self.run, self.release
        repo, start = str(release.get("repo") or ""), str(release.get("start_commit") or "")
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: the {release.get('mode') or 'cut'} of release "
            f"cycle #{release.get('cycle_id')} of project {run.project} on {repo or 'no repo'} from {start[:12]}, "
            f"{run.runtime}, timeout {run.timeout_s // 60} min of agent time. It pushes "
            f"{release_runs.BRANCH_PREFIX}<version> alone: never a default branch, never a tag."
        )
        cls = run.agent.runtime_class()
        if run.mode == "interactive":
            raise RunFailed("a release run runs headless only")
        if not repo or not releases.OBJECT_NAME.match(start):
            raise RunFailed("the claim names no repo or no commit to start the release from")
        if run.daemon.checkout_for(run.project, repo) is None:
            raise RunFailed(f"this worker has no checkout of {run.project}/{repo}")
        await access.take_credentials(run, [repo])
        run.check()
        await access.check(run, [repo], path=access.run_path(run), pushes=[repo])
        run.check()
        await self.prepare(repo, start)
        run.check()
        await self.conversation.turns(cls, self.prompt(), None)
        run.summary = verify.read_summary(run, self.directory.path)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        version, decision = self.read_version()
        diffstat = await self.push_release(version, decision)
        await run.reports.end(
            "done", diffstat=diffstat, summary=run.summary, usage=run.outcome.usage if run.outcome else None
        )

    async def prepare(self, name: str, start: str) -> None:
        """The run's directory with a writable worktree of ``name`` at ``start``, and .evo-run/ for the result."""
        directory = self.directory
        directory.claim(AGAIN)
        directory.enter()
        await directory.make_each([name], lambda repo, folder: self.make_worktree(repo, folder, start))
        (directory.path / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        directory.save()

    async def make_worktree(self, name: str, folder: str, start: str) -> gitops.Workspace:
        """A worktree of ``name`` at ``<directory>/<folder>``, on a branch of the run at commit ``start``, which the
        checkout has once origin is fetched (or once that commit is fetched by its name)."""
        run = self.run
        checkout = run.daemon.checkout_for(run.project, name)
        path = self.directory.path / folder
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await origin.fetch(run, checkout, repo=name)
            base = await gitops.rev(checkout, start)
            if base is None:
                with run.credentials.ticketed(run.daemon.env) as env:
                    await gitops.git(
                        checkout,
                        "fetch",
                        "--quiet",
                        "origin",
                        start,
                        timeout=gitops.NETWORK_TIMEOUT,
                        env=env,
                        check=False,
                    )
                base = await gitops.rev(checkout, start)
            if base is None:
                raise RunFailed(f"origin of {name} has no commit {start[:12]} to start the release from")
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(run.daemon.config, run.project, name)
            protected = tuple(sorted(origin.protected_branches(remote_head, hub_default)))
            local = origin.RUN_REPO_BRANCH.format(id=run.id, folder=folder)
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and {AGAIN}")
            try:
                await gitops.add_worktree(checkout, path, local, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        run.note(
            f"Worktree {path} of {name} on {local} at {base[:12]}, the commit the release starts from.",
            worktree=str(path),
            repo=name,
        )
        return gitops.Workspace(
            repo=name,
            branch=local,
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch=local,
            base=base,
            protected=protected,
        )

    def prompt(self) -> str:
        """The run's prompt, with where the worktree is when its folder is not named as its repo."""
        prompt = self.run.spec.get("prompt") or ""
        moved = self.directory.moved()
        extra = ["", "Worktree folders that are not named as their repo:", *moved] if moved else []
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)

    def read_version(self) -> tuple[str, str | None]:
        """The version and the Decision line the agent wrote in .evo-run/result.json; RunFailed without a version
        X.Y.Z, so nothing is pushed."""
        run = self.run
        path = self.directory.path / runs.RESULT_FILE
        if path.is_symlink() or not path.is_file():
            raise RunFailed(f"the agent did not write {runs.RESULT_FILE}: no version, nothing is pushed")
        try:
            if path.stat().st_size > RESULT_MAX_BYTES:
                raise ValueError(f"it is over {RESULT_MAX_BYTES} bytes")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunFailed(f"{runs.RESULT_FILE} was not read ({exc}): no version, nothing is pushed") from None
        data = data if isinstance(data, dict) else {}
        version = data.get(release_runs.RESULT_VERSION)
        if not isinstance(version, str) or release_runs.parse_version(version) is None:
            raise RunFailed(
                f"the agent wrote no version X.Y.Z as {release_runs.RESULT_VERSION!r} in {runs.RESULT_FILE}: nothing "
                "is pushed"
            )
        said = data.get(release_runs.RESULT_DECISION)
        decision = release_runs.one_line(said, releases.MAX_REASON_CHARS) if isinstance(said, str) else ""
        if decision:
            run.note(f"The agent picked {version}: {decision}", release_version=version)
        else:
            run.note(f"The agent picked {version} and wrote no Decision line.", release_version=version)
        return version, decision or None

    async def push_release(self, version: str, decision: str | None) -> dict:
        """Commit what the agent left and push HEAD to release/<version>, then tell the hub; the diffstat."""
        run = self.run
        ((name, workspace),) = self.directory.workspaces.items()
        path = workspace.worktree
        if not path.is_dir():
            raise RunFailed(f"the worktree of {name} at {path} is gone")
        try:
            await push.commit(run, path, name)
        except gitops.GitError as exc:
            raise RunFailed(f"committing what the agent left in {name} failed: {exc}") from None
        if await gitops.rev(path, "HEAD") == workspace.base:
            raise RunFailed(f"the agent committed nothing on {workspace.base[:12]}: there is no release to push")
        head_branch = await gitops.current_branch(path)
        if head_branch != workspace.local_branch:
            raise RunFailed(
                f"the worktree of {name} is on "
                f"{gitops.branch_ref(head_branch) if head_branch else 'a detached HEAD'}, not "
                f"{gitops.branch_ref(workspace.local_branch)}: the agent switched branches, and the worker does not "
                "push it",
                cause="branch_mismatch",
            )
        branch = release_runs.branch_of(version)
        run.check()

        async def pushed_branch() -> gitops.Pushed:
            with run.credentials.ticketed(run.daemon.env) as env:
                return await gitops.push(
                    path, branch, protected=workspace.protected, kind="release", plan_branch=None, env=env
                )

        try:
            pushed = await run.credentials.with_renewal(name, pushed_branch)
        except gitops.PushRefused as exc:
            raise RunFailed(f"{name}: {exc}") from None
        except gitops.GitError as exc:
            raise RunFailed(f"git push of {name} to {branch} failed: {exc}", cause=push_cause(exc)) from None
        run.note(
            f"Pushed {pushed.head[:12]} of {name} to {branch} on origin ({len(pushed.commits)} commit(s)).",
            commit_sha=pushed.head,
            repo=name,
        )
        await self.tell_hub(
            {"repo": name, "branch": branch, "head": pushed.head, "version": version, "decision": decision}
        )
        return await gitops.diffstat(path, workspace.base)

    async def tell_hub(self, body: dict) -> None:
        """Tell the hub the run pushed ``body``'s branch, trying a few times; RunFailed when it does not take it or
        does not answer, since nothing lands then."""
        run, branch = self.run, body["branch"]
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                await run.daemon.hub.release_branch(run.id, body)
                run.note(
                    f"The hub took {branch} of {body['repo']}: it opens the pull request, merges it and tags the merge "
                    "commit once the run is done."
                )
                return
            except Unreachable:
                await wait_or(run.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                raise RunFailed(f"the hub did not take the push of {branch}: {exc}") from None
        raise RunFailed(f"the hub did not answer: the push of {branch} was not told, so nothing lands")

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        return await self.directory.diff()

    async def release_branch(self) -> None:
        await self.directory.release()
