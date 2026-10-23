"""The kinds of run in words, for the refusals of the commands of a run's agent (``evo_agents.worker.cli``): which
run a command works in, and what the agent of another kind does instead."""

from __future__ import annotations

from evo_agents.hub import runs


def _article(kind: str) -> str:
    return "an" if kind[:1] in "aeiou" else "a"


def kinds_of(kinds: tuple[str, ...]) -> str:
    """The runs of ``kinds`` in words: "a plan run", "a plan run or an author run"."""
    return " or ".join(f"{_article(kind)} {kind} run" for kind in kinds)


def kind_refusal(run_id: int, kind: str, kinds: tuple[str, ...], command: str) -> str:
    """Why `evo-agents worker COMMAND`, which only the agent of a run of ``kinds`` uses, refuses run ``run_id`` of
    ``kind``."""
    if kinds in (("review",), ("author",)):
        return (
            f"run {run_id} is {_article(kind)} {kind} run: only the agent of {_article(kinds[0])} {kinds[0]} run uses "
            f"`evo-agents worker {command}`"
        )
    if kind == "review":
        return (
            f"run {run_id} is a review run, which reads only: it records what it finds with `evo-agents worker "
            f"finding` and `evo-agents worker propose`, not `evo-agents worker {command}`, which only the agent "
            f"of {kinds_of(kinds)} uses"
        )
    if kind == "author":
        return (
            f"run {run_id} is an author run, which writes a plan and changes nothing: it does not use "
            f"`evo-agents worker {command}`"
        )
    if kind == "judge":
        return (
            f"run {run_id} is a judge run, which reads and judges only: its verdict is the JSON object that ends "
            f"the agent's last message, not `evo-agents worker {command}`, which only the agent of "
            f"{kinds_of(kinds)} uses"
        )
    if kind == "replay":
        return (
            f"run {run_id} is a replay run, which does a step of the project's history again and reports to no plan: "
            f"leave your work in the directory and end your turn; the worker runs the step's verify, not "
            f"`evo-agents worker {command}`, which only the agent of {kinds_of(kinds)} uses"
        )
    if kind == "probe":
        return (
            f"run {run_id} is a probe run, which does one task and reports to no plan: leave your work in the "
            f"directory and end your turn; the worker checks it, not `evo-agents worker {command}`, which only the "
            f"agent of {kinds_of(kinds)} uses"
        )
    if kind == "release":
        return (
            f"run {run_id} is a release run, whose agent uses no command: it commits on the run's branch and writes "
            f"the version in {runs.RESULT_FILE}, and the worker pushes release/<version>, not `evo-agents worker "
            f"{command}`, which only the agent of {kinds_of(kinds)} uses"
        )
    if kind == "question":
        return (
            f'run {run_id} is a question run, which reads only and asks nobody: its answer is the "answer" it writes '
            f"in {runs.RESULT_FILE}, not `evo-agents worker {command}`, which only the agent of {kinds_of(kinds)} uses"
        )
    if kind == "history":
        return (
            f"run {run_id} is a history run, whose agent answers each batch it is handed with the JSON object its "
            f"prompt asks for and uses no command: `evo-agents worker {command}` is for the agent of {kinds_of(kinds)}"
        )
    return (
        f"run {run_id} is a run of one step: only the agent of {kinds_of(kinds)} uses `evo-agents worker "
        f"{command}`; a run of one step writes {runs.RESULT_FILE} instead"
    )
