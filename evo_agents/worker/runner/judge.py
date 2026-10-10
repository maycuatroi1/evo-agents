"""A judge run (kind ``judge``, ``JudgeRun``): the Curator's Judge of one change.

Once it holds its leases, the worker reads the plan's verify commands and the project's hidden checks from the hub
(GET /v1/worker/runs/{id}/judge with the run's own key, which the claim handed the daemon and only its memory holds;
the checks are held in memory, never written to a file, an event or a log line), and its preflight looks for the
program of each on PATH: one missing fails the run with missing_tool and no verdict, a hidden check named by its
number alone, so the hub has the change judged again. Its worktree is detached at the commit it judges (the pull
request's head, else the tip of the change's branch on origin), made and read with no hook of the checkout; the worker
reads the diff from the merge base with origin's default branch before any code of the change runs, finds the signs
of score hacking in it (``evo_agents.hub.judge.hack_signs``; a diff longer than the worker reads is a sign too), and
runs the hidden checks, then the verify commands, in the worktree as code it does not trust
(``evo_agents.worker.untrusted``): each one on the standard input of ``/bin/sh -s``, in a session of its own, with an
environment that holds neither the worker's home nor the run's id nor a credential, the worktree put back at the
commit judged before it, and every process it left killed after it. It then starts the Judge's agent, unless a sign
already fails the change, on the worktree put back again, the hub's prompt and what it ran (exit codes alone for the
hidden checks). The agent's verdict is the JSON object that ends its last message (``judge.verdict_from_message``),
which reaches the worker from the agent's own output: no file the code under test could write. The worker posts the
verdict with the run's key and the paths the diff touches (POST /v1/worker/runs/{id}/verdict) and ends the run done,
committing and pushing nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import judge, runs
from evo_agents.worker import credentials, gitops, untrusted
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner import access, origin
from evo_agents.worker.runner.common import RunFailed, Stopped, cut, log
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS
from evo_agents.worker.runner.verify import read_tail, verify_event

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class JudgeRun:
    """A judge run: the change at the commit it judges, its diff read for signs of score hacking, the project's hidden
    checks and the plan's verify commands run in its worktree as code the worker does not trust, the Judge's agent on
    what they found, and the verdict, which ends the agent's last message, posted; nothing committed or pushed."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        change = run.curator or {}
        run.title = run.spec.get("title") or f"judge of change #{change.get('change_id')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory)
        self.head: str | None = None
        self.merge_base: str | None = None
        self.git_dir: str | None = None  # the worktree's git directory, as it was made

    async def steps(self) -> None:
        run, change = self.run, self.run.curator or {}
        repos = [entry for entry in run.spec.get("repos") or [] if isinstance(entry, dict)]
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: the Judge of the Curator's change "
            f"#{change.get('change_id')} of project {run.project}, {run.runtime}, timeout {run.timeout_s // 60} min "
            "of agent time. It reads and judges: nothing is committed or pushed, and the Builder's transcript is not "
            "read."
        )
        cls = run.agent.runtime_class()
        if len(repos) != 1 or not repos[0].get("repo"):
            raise RunFailed("a judge run judges a change of one repo")
        name = repos[0]["repo"]
        await access.take_credentials(run, [name])
        run.check()
        inputs = await self.inputs()
        hidden = [item for item in inputs.get("hidden_checks") or [] if isinstance(item, str)]
        verify_commands = [item for item in inputs.get("verify") or [] if isinstance(item, str)]
        protected = [item for item in inputs.get("protected_paths") or [] if isinstance(item, str)]
        try:  # a program missing fails the run, not the change: it is judged again elsewhere (JUDGE_ATTEMPTS)
            await access.check(run, [name], path=self.run_path(), verify=verify_commands, hidden=hidden)
        except RunFailed:
            hidden.clear()
            inputs.clear()
            raise
        run.check()
        await self.prepare(name, change)
        run.check()
        workspace = self.directory.workspaces[name]
        files, signs = await self.read_diff(workspace, name, protected, verify_commands)
        results = await self.run_hidden(workspace.worktree, hidden)
        hidden.clear()  # the commands go: only their exit codes stay
        inputs.clear()
        checks = await self.run_checks(workspace.worktree, verify_commands)
        verdict, reasons = await self.ask_judge(cls, workspace.worktree, checks, results, signs)
        body = {
            "verdict": verdict,
            "reasons": reasons,
            "head_sha": self.head,
            "base_sha": self.merge_base,
            "verify": checks,
            "hidden": results,
            "signs": signs,
            "paths": judge.changed_paths(files)[: judge.MAX_PATHS],
        }
        await self.post(body)

    def run_path(self) -> str | None:
        """The PATH the change's verify commands and hidden checks run with: the one of the environment they get
        (``untrusted.scrubbed_env``)."""
        return untrusted.scrubbed_env(self.run.daemon.env).get("PATH")

    async def inputs(self) -> dict:
        """What the hub gives the Judge to read, asked with the run's own key; held in memory alone."""
        run = self.run
        try:
            found = await run.daemon.hub.judge_inputs(run.id, run.judge_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not give the Judge its inputs: {exc}") from None
        return found if isinstance(found, dict) else {}

    async def prepare(self, name: str, change: dict) -> None:
        """The run's directory with a worktree of ``name`` detached at the commit to judge, made with no hook of the
        checkout, and the merge base with origin's default branch, which the diff starts at."""
        run = self.run
        self.directory.claim("queue the judge run again")
        self.directory.enter()
        checkout = run.daemon.checkout_for(run.project, name)
        if checkout is None:
            raise RunFailed(f"this worker has no checkout of {run.project}/{name}")
        path = self.directory.path / gitops.folder_name(name)
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from")
            await origin.fetch(run, checkout)
            head = await self.judged_commit(checkout, name, change)
            remote_head = await gitops.remote_default_branch(checkout)
            base_branch = (
                change.get("base_branch")
                or remote_head
                or default_branch_of(run.daemon.config, run.project, name)
                or "main"
            )
            target = await gitops.rev(checkout, f"refs/remotes/origin/{base_branch}")
            base = await gitops.merge_base(checkout, target, head) if target else None
            if base is None:
                raise RunFailed(f"{name}: no merge base of {head[:12]} with origin's {base_branch}")
            try:
                await gitops.add_detached_worktree(checkout, path, head, env=gitops.without_hooks(run.daemon.env))
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
            self.git_dir = await gitops.git_dir(path)
            if self.git_dir is None:
                raise RunFailed(f"the worktree {path} has no git directory of its own")
        self.head, self.merge_base = head, base
        self.directory.workspaces[name] = gitops.Workspace(
            repo=name,
            branch=change.get("branch") or "HEAD",
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch="HEAD",
            base=head,
            protected=(),
        )
        (self.directory.path / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        self.directory.save()
        run.note(
            f"Worktree {path} of {name}, detached at {head[:12]}, the commit judged; its diff starts at {base[:12]}, "
            f"the merge base with origin's {base_branch}.",
            worktree=str(path),
        )

    async def judged_commit(self, checkout: Path, name: str, change: dict) -> str:
        """The commit to judge: the pull request's head, else the tip of the change's branch on origin; RunFailed
        when origin has neither."""
        wanted = change.get("head_sha")
        branch = change.get("branch")
        head = await gitops.rev(checkout, wanted) if judge.is_sha(wanted) else None
        if head is None and wanted:
            raise RunFailed(f"origin of {name} has no commit {wanted[:12]}: the pull request's head is not there")
        if head is None and branch:
            head = await gitops.rev(checkout, f"refs/remotes/origin/{branch}")
        if head is None:
            raise RunFailed(f"origin of {name} has no branch {branch}: there is nothing to judge")
        return head

    async def read_diff(
        self, workspace: gitops.Workspace, name: str, protected: list[str], verify_commands: list[str]
    ) -> tuple[list, list[dict]]:
        """The files of the change's diff and the signs of score hacking in it. The diff is read before any code of
        the change runs, with no textconv of the repository; one longer than the worker reads is a sign itself."""
        run = self.run
        diff, too_long = await gitops.diff_text(
            workspace.worktree, self.merge_base, self.head, env=gitops.without_hooks(run.daemon.env)
        )
        files = judge.parse_diff(diff)
        signs = judge.hack_signs(files, repo=name, protected=protected, verify_commands=verify_commands)
        if too_long:  # first, so the cap on signs never drops it
            cut_sign = {
                "kind": "diff_unreadable",
                "path": "(the diff)",
                "line": None,
                "text": f"the diff is longer than the {gitops.DIFF_TEXT_LIMIT} characters the worker reads",
            }
            signs = [cut_sign, *signs][: judge.MAX_SIGNS]
        if signs:
            kinds = ", ".join(sorted({item["kind"] for item in signs}))
            run.note(
                f"The diff shows signs of score hacking ({kinds}): the change fails, and its Judge does not start."
            )
        return files, signs

    async def ask_judge(
        self, cls, cwd: Path, checks: list[dict], results: list[dict], signs: list[dict]
    ) -> tuple[str | None, str | None]:
        """The Judge's verdict and its reasons, unless a sign already fails the change: its agent on the worktree put
        back again, the hub's prompt and what the worker ran."""
        if signs:
            return None, None
        await self.restore(cwd)
        prompt = runs.clip(self.prompt() + judge.results_text(checks, results, signs), runs.MAX_PROMPT_BYTES)
        await self.conversation.turns(cls, prompt, None)
        return self.read_verdict()

    async def post(self, body: dict) -> None:
        """Post the verdict with the run's own key, and end the run done."""
        run = self.run
        try:
            answer = await run.daemon.hub.verdict(run.id, body, run.judge_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not take the Judge's verdict: {exc}") from None
        passed = answer.get("passed") if isinstance(answer, dict) else None
        reasons = body["reasons"]
        run.summary = f"The Judge {'passed' if passed else 'failed'} the change" + (f": {reasons}" if reasons else ".")
        run.note(run.summary, passed=passed)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        usage = run.outcome.usage if run.outcome else None
        await run.reports.end("done", summary=cut(run.summary, MAX_SUMMARY_CHARS), usage=usage)

    async def restore(self, cwd: Path) -> None:
        """Put the worktree back at the commit judged: what a command of the change wrote in it before goes."""
        run = self.run
        try:
            await gitops.restore(
                cwd, self.head, expected_git_dir=self.git_dir, env=gitops.without_hooks(run.daemon.env)
            )
        except gitops.GitError as exc:
            raise RunFailed(f"the worktree {cwd} could not be put back at {self.head[:12]}: {exc}") from None

    async def run_untrusted(self, command: str, cwd: Path) -> tuple[int, str]:
        """(exit code, the end of its output) of ``command``, code of the change the worker does not trust
        (``evo_agents.worker.untrusted``): in the worktree put back at the commit judged, on the standard input of
        ``/bin/sh -s``, in a session of its own, with an environment that leads to no credential, and every process
        it left killed once it ends; Stopped when the run is asked to stop or runs out of time first."""
        run = self.run
        await self.restore(cwd)
        marker = untrusted.new_marker()
        env = untrusted.scrubbed_env(run.daemon.env, credentials.held_values())
        proc = await untrusted.start(command, cwd=cwd, env=env, marker=marker)
        tail = bytearray()
        reader = asyncio.create_task(read_tail(proc, tail))
        waiter = asyncio.create_task(proc.wait())
        stopper = asyncio.create_task(run.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, stopper},
                timeout=max(0.0, run.deadline - run.loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopper.cancel()
            killed = await untrusted.kill_leftovers(proc.pid, marker)
            if not waiter.done():
                await waiter
        if killed:
            log.info("processes a judged command left were killed", extra={"run_id": run.id, "killed": killed})
        if waiter not in done:
            reader.cancel()
            if run.stop_reason is None:
                run.stop_reason = "timeout"
            raise Stopped(run.stop_reason)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader, 5)
        reader.cancel()
        return proc.returncode, tail.decode(errors="replace")

    async def run_checks(self, cwd: Path, commands: list[str]) -> list[dict]:
        """Run each verify command of the plan in the worktree, as code the worker does not trust: each one's exit
        code."""
        run = self.run
        results = []
        if commands:
            run.note(f"Running the {len(commands)} verify command(s) of the plan.")
        for command in commands:
            run.check()
            started = run.loop.time()
            code, output = await self.run_untrusted(command, cwd)
            duration_ms = int((run.loop.time() - started) * 1000)
            results.append({"command": command, "exit_code": code, "duration_ms": duration_ms})
            run.event("system", verify_event(command, code, duration_ms, output))
        return results

    async def run_hidden(self, cwd: Path, checks: list[str]) -> list[dict]:
        """Run each hidden check of the project in the worktree, first, as code the worker does not trust: each one's
        place and exit code alone. Neither the command nor its output reaches an event, a log line, a file or an
        argument of a process."""
        run = self.run
        results = []
        for index, command in enumerate(checks, start=1):
            run.check()
            started = run.loop.time()
            code, _ = await self.run_untrusted(command, cwd)
            duration_ms = int((run.loop.time() - started) * 1000)
            results.append({"index": index, "exit_code": code, "duration_ms": duration_ms})
            run.note(f"hidden check {index} of {len(checks)} exited {code} after {duration_ms} ms")
        return results

    def read_verdict(self) -> tuple[str | None, str | None]:
        """The verdict that ends the Judge agent's last message, from its own output (``judge.verdict_from_message``);
        a file in the run's directory, which the code under test can write, counts for nothing."""
        outcome = self.run.outcome
        verdict, reasons = judge.verdict_from_message(outcome.summary if outcome is not None else None)
        if verdict is None:
            self.run.note(f"{reasons}: no verdict, which fails the change.")
            return None, None
        return verdict, reasons or None

    def prompt(self) -> str:
        """The hub's prompt, with the worktree's folder when it is not named as its repo."""
        prompt = self.run.spec.get("prompt") or ""
        moved = self.directory.moved()
        if moved:
            prompt += "\nWorktree folders that are not named as their repo:\n" + "\n".join(moved) + "\n"
        return prompt

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        """A judge run changes nothing: no diff is uploaded."""
        return b""

    async def release_branch(self) -> None:
        await self.directory.release()
