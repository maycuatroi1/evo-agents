"""The measure of a candidate of an evolve run (``evo_agents.worker.runner.evolve``) on one task, and the git it takes.

A task is a light replay task of the select split or an active probe, as the hub sends it: its prompt, its base commit
and its verify commands. Its measure (``evaluate``) lays out a new directory with the task's repository at its base
commit and the candidate's files placed (``Seams.lay_out``), runs the agent on the task's prompt there in a new session
(``Seams.agent``), then the worker runs each verify command itself in the repository (``Seams.verify``): the score is
the share of them that exited 0 (1 or 0 for a single command). What the task showed is its trace, the proposer's
reflection data: the prompt, the score, each command's exit code and whole output, the agent's failed tool calls, turns,
cost, error and last message, never the command's text. Its actionable side information (ASI, ``short_trace``) is what
the hub keeps of it with the candidate and the next mutation's prompt reads: the same, each text cut to its end.

``Seams`` is what the evolution does through the run: the agent, the verify and the directory of a task. The run's own
(``evolve.RunSeams``) drives the runtime and git; the tests hand fakes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from evo_agents.hub import evolve, probes, replay
from evo_agents.worker import gitops
from evo_agents.worker.runner import replay_files

COMMIT_AUTHOR = ("-c", "user.name=evo-agents curator", "-c", "user.email=curator@evo-agents.invalid")
SELECT = evolve.SELECT  # the split of the replay tasks an evolution measures on; a task of another is left out
TRACE_TAIL = 4000  # characters of a verify's output or the agent's last message in a task's ASI (the tree keeps all)
ASI_TOOL_ERRORS = 5  # the agent's last failed tool calls a task's ASI names
ASI_TOOL_ERROR_CHARS = 1000


@dataclass
class Session:
    """What one agent session did: its cost, turns, failed tool calls (each as text), the error that stopped it and
    its last message."""

    cost_usd: float | None = None
    turns: int | None = None
    tool_errors: list[str] = field(default_factory=list)
    error: str | None = None
    summary: str | None = None


@dataclass
class Checked:
    """What one verify command gave: its exit code and its whole output, stdout and stderr together."""

    exit_code: int
    output: str = ""


@dataclass
class Measured:
    """A task measured: its ref (``evolve.task_ref``), score, cost and turns, and its trace."""

    ref: str
    score: float
    cost_usd: float | None
    turns: int | None
    trace: dict


class Seams(Protocol):
    """What an evolution does through its run."""

    async def lay_out(self, task: Mapping, components: Mapping[str, str | None], name: str) -> tuple[Path, Path]:
        """A new directory ``name`` for the agent on ``task`` with the candidate's ``components`` placed: (the agent's
        working directory, the repository verify runs in)."""
        ...

    async def agent(self, directory: Path, prompt: str) -> Session:
        """The agent in a new session in ``directory`` on ``prompt``, its one turn over."""
        ...

    async def verify(self, command: str, cwd: Path) -> Checked:
        """``command`` run in ``cwd`` as code the worker does not trust."""
        ...


def ref_of(task: Mapping) -> str:
    """How the scores name ``task``: the ref the hub gave it, else ``evolve.task_ref``."""
    found = task.get("ref")
    return found if isinstance(found, str) and found else evolve.task_ref(task.get("kind"), task.get("id"))


def usable(task: Mapping) -> bool:
    """Whether the evolution measures on ``task``: a replay task of the select split (or of none named) or a probe,
    with a prompt and a verify. A task of the holdout split is never measured on, whatever the hub sent."""
    if task.get("kind") not in evolve.TASK_KINDS:
        return False
    if task.get("split") not in (None, SELECT):
        return False
    return bool(task.get("prompt")) and bool(verify_commands(task))


def verify_commands(task: Mapping) -> list[str]:
    """The verify commands of ``task``: one string, or a list of them."""
    found = task.get("verify")
    if isinstance(found, str):
        return [found] if found.strip() else []
    return [item for item in found or [] if isinstance(item, str) and item.strip()]


def task_prompt(task: Mapping, folder: str) -> str:
    """The prompt of the agent on ``task``: where its repository is, then the task alone."""
    return probes.probe_prompt(str(task.get("prompt") or ""), folder=folder)


async def evaluate(seams: Seams, task: Mapping, components: Mapping[str, str | None], name: str) -> Measured:
    """``task`` measured with the candidate ``components``: its directory, its agent, then each verify, run by the
    worker; the share of commands that passed as the score."""
    directory, repository = await seams.lay_out(task, components, name)
    session = await seams.agent(directory, task_prompt(task, repository.name))
    commands = verify_commands(task)
    checks = [await seams.verify(command, repository) for command in commands]
    passed = sum(check.exit_code == 0 for check in checks)
    score = passed / len(checks) if checks else 0.0
    trace = {
        "task": ref_of(task),
        "kind": task.get("kind"),
        "prompt": task.get("prompt"),
        "score": score,
        "checks": [
            {"index": index, "exit_code": check.exit_code, "passed": check.exit_code == 0, "output": check.output}
            for index, check in enumerate(checks, 1)
        ],
        "tool_errors": list(session.tool_errors),
        "turns": session.turns,
        "cost_usd": session.cost_usd,
        "agent_error": session.error,
        "summary": session.summary,
    }
    return Measured(ref_of(task), score, session.cost_usd, session.turns, trace)


def _tail(text, size: int) -> str | None:
    return str(text)[-size:] if text else None


def short_trace(trace: Mapping) -> dict:
    """A task's actionable side information (ASI), what the hub keeps of its trace with a candidate and the next
    mutation's prompt reads: the figures, the end of each failed check's output with its exit code, the agent's last
    failed tool calls, its error and the end of its last message."""
    checks = [
        {
            "exit_code": item.get("exit_code"),
            **({} if item.get("passed") else {"output_tail": str(item.get("output") or "")[-TRACE_TAIL:]}),
        }
        for item in trace.get("checks") or []
    ]
    return {
        "score": trace.get("score"),
        "checks": checks,
        "tool_errors": len(trace.get("tool_errors") or []),
        "failed_tools": [
            str(item)[:ASI_TOOL_ERROR_CHARS] for item in (trace.get("tool_errors") or [])[-ASI_TOOL_ERRORS:]
        ],
        "turns": trace.get("turns"),
        "cost_usd": trace.get("cost_usd"),
        "agent_error": trace.get("agent_error"),
        "summary": _tail(trace.get("summary"), TRACE_TAIL),
    }


# The git of an evolution


async def read_at(checkout: Path, ref: str, path: str, env: Mapping[str, str] | None = None) -> str | None:
    """The text of ``path`` at commit ``ref`` of ``checkout``, None when that commit has no such file."""
    code, out, _ = await gitops.git(checkout, "show", f"{ref}:{path}", check=False, env=env)
    return out if code == 0 else None


async def baseline_at(checkout: Path, ref: str, paths: list[str], env: Mapping[str, str] | None = None) -> dict:
    """path -> its whole text at ``ref`` for each of ``paths`` and every file of the directory of a skill one of them
    is a file of; a path ``ref`` lacks is left out."""
    wanted = list(dict.fromkeys(paths))
    for skill in sorted({found for found in map(replay.skill_dir, paths) if found}):
        _, out, _ = await gitops.git(checkout, "ls-tree", "-r", "-z", "--name-only", ref, "--", skill, env=env)
        wanted += [path for path in out.split("\0") if path and path not in wanted]
    found = {}
    for path in wanted:
        text = await read_at(checkout, ref, path, env)
        if text is not None:
            found[path] = text
    return found


def placed(baseline: Mapping[str, str | None], components: Mapping[str, str | None]) -> dict[str, str | None]:
    """What a candidate writes over the baseline: each of its components, and None (removed) for each baseline file it
    has not."""
    return {**dict.fromkeys(baseline), **components}


async def commit_candidate(
    worktree: Path, texts: Mapping[str, str | None], message: str, env: Mapping[str, str] | None = None
) -> str | None:
    """Write ``texts`` (path -> whole text, None to remove it) in ``worktree`` and commit them; the new commit, None
    when they change nothing."""
    replay_files.write_texts(texts, worktree)
    await gitops.git(worktree, "add", "--all", env=env)
    code, _, _ = await gitops.git(worktree, "diff", "--cached", "--quiet", check=False, env=env)
    if code == 0:
        return None
    await gitops.git(worktree, *COMMIT_AUTHOR, "commit", "--quiet", "--no-verify", "-m", message, env=env)
    return await gitops.rev(worktree, "HEAD")
