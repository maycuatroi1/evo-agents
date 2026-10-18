"""A task run (kind ``task``, ``TaskRun``, ``evo_agents.hub.task_runs``): one headless turn of a plugin's agent type on
a task the plugin handed it, which may change code.

The daemon takes the run's leases for the repos of its project it has a checkout of (the others it leaves out, saying
so), checks it can read each, and makes the directory ``~/.evo/worker/worktrees/<project>-<run>`` with a writable
worktree of each, on the run's branch ``task/<id>`` (the id of the run the plugin's dispatch made), from origin's
branch when an earlier attempt pushed it already, else from origin's default branch (``branch_work``). The agent works
in EVO_RUN_KIND ``task``, one turn that asks nobody anything. When it ends, the daemon commits what the agent left in
each worktree and pushes the run's branch of each repo whose commits the branch lacks on origin, never a default branch
and never forced (``gitops.check_push``, kind ``task``), tells the hub (POST /v1/worker/runs/{id}/task/pulls), which
opens a draft pull request of it and never merges, and reports ``done``; the hub tells the plugin (``task.result``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs, task_runs
from evo_agents.worker.runner import access, verify
from evo_agents.worker.runner.branch_work import BranchWork, Kind
from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

TASK = Kind("task", "task/pulls", "as a draft pull request", "let the plugin hand the task again")


async def _no_more_turns() -> list[dict]:
    """A task run is one turn: nobody writes to its agent."""
    return []


class TaskRun:
    """A task run: a writable worktree of each repo of its project this worker has a checkout of, on the run's branch,
    one headless turn of the agent on the task, and the branch of each repo it changed pushed at its end, with the hub
    told so for the draft pull request."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"task run of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=_no_more_turns)
        self.branch = str(run.spec.get("branch") or task_runs.branch_of(run.id))
        self.work = BranchWork(run, self.directory, self.branch, TASK)  # its worktrees and their push

    async def steps(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: a task run of project {run.project} on "
            f"{self.branch}, {run.runtime}, timeout {run.timeout_s // 60} min of agent time."
        )
        cls = run.agent.runtime_class()
        if run.mode == "interactive":
            raise RunFailed("a task run runs headless only")
        if not task_runs.is_task_branch(self.branch):
            raise RunFailed(f"a task run works on a branch task/<run>, not {self.branch}")
        held = self.work.held_repos(repos)
        names = [entry["repo"] for entry in held]
        await access.take_credentials(run, names)
        run.check()
        await access.check(run, names, path=access.run_path(run))
        run.check()
        await self.work.prepare(held)
        await self.conversation.turns(cls, self.prompt(), None)
        run.summary = verify.read_summary(run, self.directory.path)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        diffstat = await self.work.push_repos()
        await run.reports.end(
            "done", diffstat=diffstat, summary=run.summary, usage=run.outcome.usage if run.outcome else None
        )

    def prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        prompt = self.run.spec.get("prompt") or ""
        extra = self.work.where()
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        return await self.directory.diff()

    async def release_branch(self) -> None:
        """The worktrees stay on the run's branch until the cleanup of old runs: the next attempt starts from what
        was pushed of it."""
