"""A run of one step (kind ``step``, ``StepRun``), from the claim to its last report.

1. ``leased``: the daemon takes the run's leases (``credentials``), so its git and its agent use them, then checks what
   the run needs before it fetches anything (``access.check``, ``preflight``): the project lists an origin for the
   repo, git reads it and pushes to it with what the run holds, and each program the step's verify calls is on the
   run's PATH; a check that does not pass fails the run before the agent starts, with its cause (the report's
   ``failure_cause``: origin, credentials or missing_tool). It then fetches origin in the checkout of the run's repo
   and makes a worktree under ``~/.evo/worker/worktrees/<project>-<run>``, on the plan's branch for the repo
   (``origin``: from ``origin/<branch>`` when the remote has it, on ``evo-run/<run>`` when that branch is checked out
   elsewhere or has commits here that the start lacks, the push still going to the plan's branch). A plan without a
   branch for the repo, or whose branch is the default branch (the remote's HEAD, the hub's default_branch, main or
   master), fails the run before the agent starts: the daemon never pushes the default branch.
2. ``running``: the runtime's adapter starts the agent on the run's prompt; its events go to the spool and on to
   the hub. The owner's messages reach the agent through ``send``; a cancel, the run's timeout (counted from the
   agent's start), or the hub no longer holding the run for this worker interrupt it. ``interactive``: after a
   takeover, or from the start in interactive mode, a person drives the agent's session in its runtime's terminal UI
   in tmux (``terminal``), and a handback, or the person leaving the UI, lets a new adapter go on headless in the
   same session (``running`` again).
3. ``verifying``: the agent wrote ``.evo-run/result.json``; the daemon runs each of its ``verify_commands`` again in
   the worktree (``verify``). A command that exits other than 0 fails the run, and nothing is pushed: the work stays
   in the worktree.
4. The daemon commits what the agent left uncommitted (``push.commit``); refuses to push a detached HEAD or a branch
   the agent switched to, pushes the plan's branch to origin (never forced, never merged), and reports ``done``
   (approval ``auto``) or ``review`` with the commit, the diffstat, the verify results, the agent's summary and usage.
   Every event is sent before that report.
5. After the last report the log (the run's events) and the diff are uploaded as blobs when the hub has a blob store,
   and the worktree leaves the plan's branch, so the owner can check it out elsewhere; it is removed 7 days later.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import gitops
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.runner import access, origin, push, verify
from evo_agents.worker.runner.common import RunFailed, push_cause
from evo_agents.worker.runner.transitions import final_state, start_refs, verify_failure

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class StepRun:
    """A run of one step: one worktree on the plan's branch, the agent in it, its verify commands run again, and the
    branch pushed."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"step {run.spec.get('step_key')}"
        self.checkout: Path | None = None
        self.local_branch: str | None = None
        self.base: str | None = None
        self.protected: set[str] = set()

    async def steps(self) -> None:
        run, spec = self.run, self.run.spec
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: step {spec.get('step_key')} of plan "
            f"{spec.get('plan_id')}, {run.runtime}, {run.mode}, approval {run.approval}, timeout "
            f"{run.timeout_s // 60} min."
        )
        cls = run.agent.runtime_class()
        run.agent.check_interactive(cls)
        checkout = run.daemon.checkout_for(run.project, run.repo)
        if checkout is None:
            raise RunFailed(f"this worker has no checkout of {run.project}/{run.repo}")
        if not run.branch:
            raise RunFailed(
                f"the plan names no branch for {run.repo}, and the worker never works on the default branch"
            )
        await access.take_credentials(run, [run.repo])
        run.check()
        await access.check(run, [run.repo], path=access.run_path(run), pushes=[run.repo], verify=self.claimed_verify())
        run.check()
        await self.prepare(checkout)
        run.check()
        await self.run_agent(cls)
        commands = verify.read_result(run)
        await run.reports.report("verifying")
        run.check()
        run.verify = await verify.run_verify(run, commands)
        failure = verify_failure(run.verify, run.worktree)
        if failure is not None:
            error, cause = failure
            raise RunFailed(error, verify=run.verify, usage=run.outcome.usage if run.outcome else None, cause=cause)
        run.check()
        commit_sha, diffstat = await self.commit_and_push()
        await run.reports.end(
            final_state(run.approval),
            commit_sha=commit_sha,
            diffstat=diffstat,
            verify=run.verify,
            summary=run.summary,
            usage=run.outcome.usage if run.outcome else None,
        )

    def claimed_verify(self) -> list[str]:
        """The verify of a run of one step as its claim names it (``RunSpec.verify``); none from an older hub."""
        return [item for item in self.run.spec.get("verify") or [] if isinstance(item, str) and item.strip()]

    async def prepare(self, checkout: Path) -> None:
        """The run's worktree of ``checkout``, the checkout of its repo, on the plan's branch."""
        run = self.run
        branch = run.branch
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await origin.fetch(run, checkout)
            if not await gitops.check_branch_name(checkout, branch):
                raise RunFailed(f"the plan's branch {branch!r} is not a valid branch name")
            remote_head = await gitops.remote_default_branch(checkout)
            self.protected = origin.protected_branches(
                remote_head, default_branch_of(run.daemon.config, run.project, run.repo)
            )
            if branch in self.protected:
                raise RunFailed(
                    f"the plan's branch for {run.repo} is {branch}, a default branch: the worker never pushes it; "
                    "give the repo a branch of its own in the plan"
                )
            start, base = await origin.start_point(checkout, start_refs(branch))
            if start is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to start from")
            path = run.daemon.home.worktree_path(run.project, run.id)
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and run the step again")
            local_branch, why = await origin.side_or_branch(checkout, branch, base, origin.RUN_BRANCH.format(id=run.id))
            if why is not None:
                run.note(f"{branch} {why}; the run works on {local_branch} and pushes it to {branch}.")
            try:
                await gitops.add_worktree(checkout, path, local_branch, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        self.checkout, run.worktree, self.local_branch, self.base = checkout, path, local_branch, base
        run.record.update(
            {"checkout": str(checkout), "worktree": str(path), "local_branch": local_branch, "base": base}
        )
        run.daemon.home.save_run(run.record)
        run.note(f"Worktree {path} on {local_branch} at {base[:12]} ({start}).", worktree=str(path))

    async def run_agent(self, cls) -> None:
        """Run the agent until its last turn is over (``Agent.turns``), on the run's prompt."""
        run = self.run
        run.outcome, _ = await run.agent.turns(cls, run.spec["prompt"], None, terminal_first=run.mode == "interactive")
        if not run.outcome.completed:
            raise RunFailed(
                run.outcome.error or f"{run.runtime} ended before its turn completed",
                usage=run.outcome.usage,
                cap=run.outcome.cap,
            )

    async def commit_and_push(self) -> tuple[str, dict]:
        """Commit what the agent left, and push the worktree's branch to the plan's branch on origin; the commit and
        the diffstat of the run."""
        run = self.run
        wt = run.worktree
        try:
            await push.commit(run, wt)
        except gitops.GitError as exc:
            raise RunFailed(f"committing what the agent left failed: {exc}", verify=run.verify) from None
        head_branch = await gitops.current_branch(wt)
        if head_branch is None:
            raise RunFailed(
                "HEAD is detached in the worktree: the worker does not push a detached HEAD", verify=run.verify
            )
        if head_branch != self.local_branch:
            raise RunFailed(
                f"the worktree is on {head_branch}, not {self.local_branch}: the agent switched branches, and the "
                "worker does not push it",
                verify=run.verify,
            )
        commit_sha = await gitops.rev(wt, "HEAD")
        diffstat = await gitops.diffstat(wt, self.base)
        run.check()

        async def pushed_branch() -> gitops.Pushed:
            with run.credentials.ticketed(run.daemon.env) as env:
                return await gitops.push(wt, run.branch, protected=self.protected, kind="step", env=env)

        try:  # a default branch was refused before the agent started; gitops refuses it again before every push
            pushed = await run.credentials.with_renewal(run.repo, pushed_branch)
        except gitops.PushRefused as exc:
            raise RunFailed(str(exc), verify=run.verify) from None
        except gitops.GitError as exc:
            raise RunFailed(
                f"git push to {run.branch} on origin failed: {exc}", verify=run.verify, cause=push_cause(exc)
            ) from None
        if pushed.changed:
            run.note(
                f"Pushed {commit_sha[:12]} to {run.branch} on origin ({diffstat['files']} file(s), "
                f"+{diffstat['insertions']} -{diffstat['deletions']}).",
                commit_sha=commit_sha,
            )
        else:
            run.note(f"{run.branch} on origin has {commit_sha[:12]} already: nothing to push.", commit_sha=commit_sha)
        run.record.update({"commit_sha": commit_sha, "pushed": True})
        return commit_sha, diffstat

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        run = self.run
        if run.worktree is None or self.base is None or not run.repo:
            return []
        return [(run.repo, run.worktree, self.base)]

    async def diff(self) -> bytes:
        """The diff of the run's commits, empty when there is none."""
        if self.run.worktree is None or self.base is None:
            return b""
        with contextlib.suppress(gitops.GitError, OSError, asyncio.TimeoutError):
            return await gitops.diff(self.run.worktree, self.base)
        return b""

    async def release_branch(self) -> None:
        """Leave the plan's branch once the run is over, so it can be checked out elsewhere."""
        run = self.run
        if run.worktree is None or self.local_branch != run.branch or not run.worktree.exists():
            return
        with contextlib.suppress(gitops.GitError, OSError):
            await gitops.detach(run.worktree)
