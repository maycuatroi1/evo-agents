"""The Python environment of each worktree of a plan run or a run of one step whose repo asks for one
(``evo_agents.worker.venvs``): made once the worktrees are, before the agent starts, and made again before the daemon
runs a step's verify.

Each repo that asks gets ``uv sync --frozen`` (and its requirement files) in its worktree, one repo after the other,
in the run's time; its ``.venv/bin`` goes first on the PATH of the agent and of the commands the daemon runs for the
run (``Agent.env``), in the run's order of its repos, and in the run's record (``venvs``, repo -> that directory), where
``evo-agents worker step`` reads it. A repo whose settings are not read, or whose environment is not made, fails the
run before the agent starts with cause environment; uv not on the run's PATH fails it with cause missing_tool. A run
whose agent runs in a container gets no environment of the host: the run's log says so.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import venvs
from evo_agents.worker.runner import verify
from evo_agents.worker.runner.common import RunFailed, cut

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

OUTPUT_CHARS = 1500  # the end of a failed command's output in the run's error


async def prepare(run: Run, worktrees: list[tuple[str, Path]]) -> None:
    """Make the environment of each of ``worktrees`` (repo, worktree) whose repo asks for one, and put it on the run's
    PATH (``Run.venvs``) and in its record."""
    asked: list[tuple[str, Path, venvs.Wanted]] = []
    for name, worktree in worktrees:
        want = wanted(name, worktree)
        if want is not None:
            asked.append((name, worktree, want))
    run.venvs.clear()
    if asked and _in_container(run):
        names = ", ".join(name for name, _, _ in asked)
        run.note(f"{names} ask for a Python environment of the worktree; the agent runs in a container: none is made.")
        asked = []
    for name, worktree, want in asked:
        await make(run, name, worktree, want, "the agent did not start")
        run.venvs[name] = worktree
        run.note(f"{venvs.bin_dir(worktree)} is first on the PATH of the run's agent and of its verify.", repo=name)
    run.record["venvs"] = {name: str(venvs.bin_dir(worktree)) for name, worktree in run.venvs.items()}
    run.daemon.home.save_run(run.record)


async def refresh(run: Run) -> None:
    """Make each environment of the run again, so what the agent changed in its repo's pyproject.toml (an entry point,
    a dependency) is in it before the daemon runs the verify."""
    for name, worktree in run.venvs.items():
        want = wanted(name, worktree)
        if want is not None:
            await make(run, name, worktree, want, "the verify did not run")


def wanted(name: str, worktree: Path) -> venvs.Wanted | None:
    try:
        return venvs.wanted(worktree)
    except ValueError as exc:
        raise RunFailed(f"{name}: {exc}", cause="environment") from None


async def make(run: Run, name: str, worktree: Path, want: venvs.Wanted, then: str) -> None:
    """Run the commands of ``want`` in ``worktree``; RunFailed, its error ending in ``then``, when uv is not on the
    run's PATH or one fails."""
    env = venvs.uv_env(run.agent.env())
    uv = shutil.which("uv", path=env.get("PATH"))
    if uv is None:
        raise RunFailed(
            f"{name} asks for a Python environment of its worktree ([tool.evo-agents.worker] of pyproject.toml), "
            f"and uv is not on this worker's PATH; {then}",
            cause="missing_tool",
        )
    for command in want.commands(uv):
        started = run.loop.time()
        code, output = await verify.shell(run, shlex.join(command), run.deadline - started, cwd=worktree, env=env)
        duration_ms = int((run.loop.time() - started) * 1000)
        if code != 0:
            tail = cut(output.strip()[-OUTPUT_CHARS:], OUTPUT_CHARS)
            raise RunFailed(
                f"`{venvs.shown(command)}` in {name} exited {code}: {tail}; {then}",
                cause="environment",
            )
        run.note(f"`{venvs.shown(command)}` in {name}: {duration_ms} ms.", repo=name)


def _in_container(run: Run) -> bool:
    try:
        return bool(run.agent.environment.container)
    except ValueError:  # an environment this worker does not read: the preflight failed the run already
        return False
