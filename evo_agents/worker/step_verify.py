"""The verify and push-conflict stages of ``evo-agents worker step`` (``cli.cmd_step``).

``agent`` is the command's ``cli._AgentRun``: the worker's home, its config and the run's record.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from evo_agents.worker.home import WorkerStateError

OUTPUT_TAIL = 4000  # characters of a failed verify command's output printed


async def verify_env(agent, name: str, path: Path) -> dict[str, str] | None:
    """The environment of the verify of a done step in repo ``name``, whose worktree is ``path``: when the daemon made
    the repo's Python environment (``evo_agents.worker.venvs``), it is made again, so what the step changed in
    pyproject.toml is in it, and its bin directory goes first on PATH; else None, the agent's own environment."""
    from evo_agents.worker import venvs

    made = agent.record.get("venvs")
    bin_dir = made.get(name) if isinstance(made, dict) else None
    if not isinstance(bin_dir, str) or not bin_dir:
        return None
    env = venvs.uv_env(os.environ)
    try:
        want = venvs.wanted(path)
    except ValueError as exc:
        raise WorkerStateError(f"{name}: {exc}") from None
    uv = shutil.which("uv", path=env.get("PATH"))
    if want is not None and uv is None:
        print(f"warning: uv is not on PATH: the Python environment of {name} is not made again", file=sys.stderr)
    elif want is not None:
        command, code, output = await venvs.make(path, want, uv, env)
        if code != 0:
            tail = output.rstrip()[-OUTPUT_TAIL:]
            raise WorkerStateError(
                f"`{venvs.shown(command)}` in {name} exited {code}, so the verify did not run:\n{tail}"
            )
        said = ", ".join(f"`{venvs.shown(command)}`" for command in want.commands())
        print(f"Made the Python environment of {name} again: {said}.")
    env["PATH"] = venvs.front(env.get("PATH"), [bin_dir])
    return env


async def verify(path: Path, commands: list[str], env: dict[str, str] | None = None) -> list[dict]:
    """Run each command with /bin/sh in ``path``, in ``env`` (this process's when None), every one even after a
    failure; each one's exit code."""
    results = []
    for command in commands:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        output, _ = await proc.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        results.append({"command": command, "exit_code": proc.returncode, "duration_ms": duration_ms})
        print(f"verify: `{command}` exited {proc.returncode} after {duration_ms} ms")
        if proc.returncode != 0:
            tail = output.decode(errors="replace")[-OUTPUT_TAIL:].rstrip()
            if tail:
                print("\n".join(f"  | {line}" for line in tail.splitlines()))
    return results


async def push_conflict(hub, agent, name: str, conflict) -> None:
    """A push of ``worker step`` went to the run's side branch (``gitops.PushConflict``): tell the run's owner, and
    leave the daemon the word to fail the run once the agent's turn ends."""
    from evo_agents.worker import gitops
    from evo_agents.worker.hubapi import HubProblem

    agent.home.save_conflict(
        agent.run_id,
        {
            "error": f"{name}: {conflict}",
            "repo": name,
            "branch": conflict.branch,
            "side_branch": conflict.side_branch,
            "head": conflict.head,
        },
    )
    try:
        await hub.notice(agent.run_id, gitops.conflict_notice(agent.run_id, name, conflict))
        print(f"Notified the run's owner that the commits of {name} are on {conflict.side_branch}.", file=sys.stderr)
    except HubProblem as exc:
        print(f"warning: the notice of {conflict.side_branch} was not sent ({exc})", file=sys.stderr)
