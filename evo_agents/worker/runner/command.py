"""A command run (kind ``command``, ``CommandRun``, ``evo_agents.hub.command_runs``): the shell command line of a cron
job, run with the machine's shell and no agent.

The daemon takes the run's leases for its one repo, when it names one (every lease a run of its owner gets for that
repo, less the model accounts the hub never leases it), checks it reads that repo, and makes the directory
``~/.evo/worker/worktrees/<project>-<run>`` with a worktree of the repo detached at origin's default branch; a run with
no repo gets that directory empty. There it runs the command line with ``$SHELL -c`` (else /bin/sh), in a session of
its own, with the environment the daemon gives any run's commands (``Agent.env``) but no model's key, and stdin closed.
Each line of its stdout and stderr goes to the run's log as an ``output`` event, at most
``command_runs.MAX_OUTPUT_LINES`` lines of at most ``command_runs.MAX_LINE_CHARS`` characters. Exit 0 reports
``done``; any other code fails the run with cause ``command_failed``. A cancel, or the run's timeout, which counts from
the command's start, stops the command's whole process group (SIGTERM, then SIGKILL) and ends the run as any stopped
run ends: a timeout fails it. The daemon commits and pushes nothing the command left; its worktree goes with the
cleanup of old runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import command_runs
from evo_agents.hub.account_providers import MODEL_ENV_VARS
from evo_agents.worker.runner import access
from evo_agents.worker.runner.common import RunFailed, Stopped
from evo_agents.worker.runner.directory import RunDirectory

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

POLL = 0.5  # seconds between two checks of the run (cancel, timeout) while the command runs
GRACE = 5.0  # seconds the command's processes have to end after SIGTERM, before SIGKILL
CHUNK = 64 * 1024  # bytes read of a stream at a time


def shell_of(env: dict[str, str]) -> str:
    """The machine's shell: ``SHELL`` of the run's environment when it names a program, else /bin/sh."""
    shell = env.get("SHELL") or ""
    return shell if os.path.isabs(shell) and os.access(shell, os.X_OK) else "/bin/sh"


class CommandRun:
    """A command run: the cron job's command line in a worktree of its repo, or in an empty directory, its output to the
    run's log and its exit code the run's end."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"command run of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.lines = 0  # lines of output logged
        self.dropped = 0  # lines past MAX_OUTPUT_LINES, counted only

    def repo(self) -> str | None:
        """The repo the command runs in: the one entry of the run's repos; None when it names none."""
        names = access.repo_names(
            entry.get("repo") for entry in self.run.spec.get("repos") or [] if isinstance(entry, dict)
        )
        if len(names) > 1:
            raise RunFailed(f"a command run names one repo at most, not {len(names)}")
        return names[0] if names else None

    async def steps(self) -> None:
        run = self.run
        command = run.spec.get("command")
        problem = command_runs.command_problem(command)
        if problem is not None:
            raise RunFailed(f"the run holds no command to run: {problem}")
        repo = self.repo()
        where = f"a worktree of {repo} at its default branch" if repo else "an empty directory"
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: a command run of project {run.project} in "
            f"{where}, timeout {run.timeout_s // 60} min. The machine's shell runs it; no agent, no model."
        )
        run.check()
        await access.take_credentials(run, [repo] if repo else [])  # cloning the repo first when it lacks one
        run.check()
        if repo:
            await access.check(run, [repo], path=access.run_path(run))
            run.check()
        self.directory.claim("let the cron job fire again")
        self.directory.enter()
        if repo:
            checkout = run.daemon.checkout_for(run.project, repo)
            if checkout is None:
                raise RunFailed(f"this worker has no checkout of {run.project}/{repo}")
            await self.directory.make_each([repo], lambda name, folder: self.directory.detached(name, checkout, folder))
        self.directory.save()
        run.check()
        cwd = self.directory.workspaces[repo].worktree if repo else self.directory.path
        code = await self.execute(command, cwd)
        summary = self.summary(code)
        if code != 0:
            raise RunFailed(summary, cause=command_runs.FAILURE_CAUSE)
        await run.reports.report("verifying")  # the way to done: the hub takes no done from running
        await run.reports.end("done", summary=summary)

    def env(self) -> dict[str, str]:
        """The command's environment: the one the daemon gives any run's commands, with no model's key."""
        env = self.run.agent.env()
        for key in MODEL_ENV_VARS:
            env.pop(key, None)
        return env

    async def execute(self, command: str, cwd: Path) -> int:
        """Run ``command`` in ``cwd`` and log its output; its exit code. Stopped, with its processes gone, when the run
        is cancelled or its timeout passes first."""
        run = self.run
        env = self.env()
        shell = shell_of(env)
        await run.reports.report("running", only_from=("leased",))
        run.agent_starts()  # the timeout counts from the command's start
        process = await asyncio.create_subprocess_exec(
            shell,
            "-c",
            command,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, which a stop ends whole
        )
        run.note(f"Command started with {shell} in {cwd} (pid {process.pid}).", pid=process.pid)
        readers = [
            asyncio.create_task(self.read(process.stdout, "stdout")),
            asyncio.create_task(self.read(process.stderr, "stderr")),
        ]
        try:
            while True:
                run.check()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(process.wait()), POLL)
                if process.returncode is not None:
                    break
        except (Stopped, asyncio.CancelledError) as exc:
            await self.kill(process)
            reason = exc.reason if isinstance(exc, Stopped) else "shutdown"
            run.note(f"Command stopped ({reason}): its processes were ended.")
            raise
        finally:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), GRACE)
            for reader in readers:
                reader.cancel()
        return process.returncode

    async def kill(self, process: asyncio.subprocess.Process) -> None:
        """End the command's process group: SIGTERM, then, past GRACE, SIGKILL of whatever is left of it."""
        for sent in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, sent)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), GRACE)

    async def read(self, stream: asyncio.StreamReader | None, name: str) -> None:
        """Log each line of ``stream`` (stdout or stderr, ``name``) as it comes."""
        if stream is None:
            return
        held = b""
        while True:
            chunk = await stream.read(CHUNK)
            if not chunk:
                break
            held += chunk
            *lines, held = held.split(b"\n")
            for line in lines:
                self.log(name, line)
            if len(held) > command_runs.MAX_LINE_CHARS * 4:  # a line with no end yet: log what came of it
                self.log(name, held)
                held = b""
        if held:
            self.log(name, held)

    def log(self, name: str, line: bytes) -> None:
        if self.lines >= command_runs.MAX_OUTPUT_LINES:
            self.dropped += 1
            return
        text = line.decode("utf-8", errors="replace").rstrip("\r")
        if len(text) > command_runs.MAX_LINE_CHARS:
            text = text[: command_runs.MAX_LINE_CHARS - 3] + "..."
        self.lines += 1
        self.run.event("output", {"stream": name, "line": text})

    def summary(self, code: int) -> str:
        """What the run's end says: the exit code, and the lines of output logged and left out."""
        ended = f"exited {code}" if code >= 0 else f"was killed by signal {-code}"
        said = f"{self.lines} line(s) of output"
        if self.dropped:
            said += f", and {self.dropped} more left out of the log"
        return f"The command {ended}, after {said}."

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        return await self.directory.diff()

    async def release_branch(self) -> None:
        """The worktree is detached: no branch to leave. It stays until the cleanup of old runs."""
        return None
