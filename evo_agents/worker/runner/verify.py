"""The agent's result file, and the verify commands the daemon runs again in a run's worktree.

The agent of a run of one step writes ``.evo-run/result.json``; the daemon runs each of its ``verify_commands`` again
in the worktree, with the run's time left, and records each exit code as a ``system`` event. A command that exits
other than 0 fails the run, and nothing is pushed: the work stays in the worktree. A run of a plan, a review or an
author run reads only the agent's summary from that file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker.runner.common import RunFailed, Stopped, cut
from evo_agents.worker.runner.transitions import result_commands, summary_of

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

RESULT_MAX_BYTES = 1024 * 1024
OUTPUT_TAIL = 8 * 1024  # bytes of a verify command's output kept in its event


def read_result(run: Run) -> list[str]:
    """The verify commands of the result file the agent wrote in the run's worktree; the run's summary from it.
    RunFailed when the file is missing, too large, not JSON, or lists no valid verify command."""
    path = run.worktree / runs.RESULT_FILE
    if path.is_symlink() or not path.is_file():
        raise RunFailed(f"the agent did not write {runs.RESULT_FILE}", usage=run.outcome.usage)
    if path.stat().st_size > RESULT_MAX_BYTES:
        raise RunFailed(f"{runs.RESULT_FILE} is over {RESULT_MAX_BYTES} bytes")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunFailed(f"{runs.RESULT_FILE} is not JSON: {exc}") from None
    commands = result_commands(data)
    run.summary = summary_of(data)
    return commands


def read_summary(run: Run, directory: Path) -> str | None:
    """The agent's summary from .evo-run/result.json in ``directory``; a run of several repos lists no verify
    commands."""
    path = directory / runs.RESULT_FILE
    if path.is_symlink() or not path.is_file():
        run.note(f"The agent wrote no {runs.RESULT_FILE}: the run ends without its summary.")
        return None
    try:
        if path.stat().st_size > RESULT_MAX_BYTES:
            raise ValueError(f"it is over {RESULT_MAX_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        run.note(f"{runs.RESULT_FILE} was not read ({exc}): the run ends without its summary.")
        return None
    return summary_of(data)


def verify_event(command: str, code: int, duration_ms: int, output: str) -> dict:
    """The ``system`` event of a verify command that ran."""
    return {
        "text": f"verify: `{cut(command, 200)}` exited {code} after {duration_ms} ms",
        "command": command,
        "exit_code": code,
        "duration_ms": duration_ms,
        "output": output,
    }


async def run_verify(run: Run, commands: list[str]) -> list[dict]:
    """Run each of ``commands`` again in the run's worktree: each one's exit code and time."""
    results = []
    run.note(f"Running the {len(commands)} verify command(s) of {runs.RESULT_FILE} again.")
    for command in commands:
        run.check()
        started = run.loop.time()
        code, output = await shell(run, command, run.deadline - started)
        duration_ms = int((run.loop.time() - started) * 1000)
        results.append({"command": command, "exit_code": code, "duration_ms": duration_ms})
        run.event("system", verify_event(command, code, duration_ms, output))
    return results


async def shell(run: Run, command: str, seconds: float, cwd: Path | None = None) -> tuple[int, str]:
    """(exit code, the end of its output) of ``command`` in the run's worktree (or ``cwd``), in the agent's
    environment; Stopped when the run is asked to stop or runs out of time first, with the command's process group
    killed."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd or run.worktree),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=run.agent.env(),
        start_new_session=True,
    )
    tail = bytearray()
    reader = asyncio.create_task(read_tail(proc, tail))
    waiter = asyncio.create_task(proc.wait())
    stopper = asyncio.create_task(run.stop_event.wait())
    try:
        done, _ = await asyncio.wait({waiter, stopper}, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
    if waiter not in done:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await waiter
        reader.cancel()
        if run.stop_reason is None:
            run.stop_reason = "timeout"
        raise Stopped(run.stop_reason)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(reader, 5)
    return proc.returncode, tail.decode(errors="replace")


async def read_tail(proc, tail: bytearray) -> None:
    """Read the output of ``proc`` into ``tail``, keeping its last OUTPUT_TAIL bytes."""
    while chunk := await proc.stdout.read(65536):
        tail.extend(chunk)
        del tail[:-OUTPUT_TAIL]
