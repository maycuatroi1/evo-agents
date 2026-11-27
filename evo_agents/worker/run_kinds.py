"""What each kind of run may do, declared once (``Kind``) and read by name (``of``), with no import of the kinds' own
classes: the daemon's run kinds (``runner.kinds``) each declare theirs (``RunKind.traits``), and the commands of a
run's agent (``evo_agents.worker.cli``), which know the kind only by EVO_RUN_KIND, read the same.

A kind says what it pushes (``push_rules``), whether its agent may run in a container, whether someone at this machine
answers its agent's questions, whether the hub takes its waits, whether its prompts are the owner's words, whether its
leases stay out of its agent's reach, which commands of a run's agent it uses, whether its record names a directory of
worktrees, and, in words, what its agent does instead of a command it does not use (``refusal``)."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub import runs
from evo_agents.worker import push_rules
from evo_agents.worker.push_rules import PushRule


def _article(kind: str) -> str:
    return "an" if kind[:1] in "aeiou" else "a"


def kinds_of(kinds: tuple[str, ...]) -> str:
    """The runs of ``kinds`` in words: "a plan run", "a plan run or an author run"."""
    return " or ".join(f"{_article(kind)} {kind} run" for kind in kinds)


ONE_STEP = (
    "run {run} is a run of one step: only the agent of {kinds} uses `evo-agents worker {command}`; a run of one step "
    "writes {result} instead"
)


@dataclass(frozen=True)
class Kind:
    """What a kind of run may do (see the module)."""

    name: str
    refuses: str = ONE_STEP  # why a command its agent does not use refuses it: {run}, {command}, {kinds}, {result}
    pushes: PushRule = push_rules.OTHER
    container: bool = False  # its agent needs no command of this daemon, so it may run in a container
    answered: bool = True  # someone at this machine answers its agent's question tool
    waits: bool = False  # the hub takes its waits: the wait guard refuses its watches, and its session has no Monitor
    verbatim: bool = False  # its prompts are the owner's words: an @handle or @path in them never reads a file
    guarded: bool = False  # its agent never holds a push credential: only the daemon's own git gets its leases
    hub_server: bool = False  # its agent calls the hub's tools (``runtimes.hub_tools``), whoever runs it
    talks: bool = False  # its agent talks in the run's chat (``worker chat``, ``worker agent``)
    asks_in_chat: bool = False  # its agent ends its turn with a message to the member once it asked its decisions
    parks: bool = False  # it waits for its owner between turns, and the hub may park it
    builds: bool = False  # its agent may be the Builder of a Curator's change (``worker curator drop``)
    directory: bool = True  # its record names a directory with a worktree of each repo; else one worktree, or none

    def refusal(self, run_id: int, command: str, kinds: tuple[str, ...]) -> str:
        """Why `evo-agents worker COMMAND`, which only the agent of a run of ``kinds`` uses, refuses run ``run_id`` of
        this kind."""
        return self.refuses.format(run=run_id, command=command, kinds=kinds_of(kinds), result=runs.RESULT_FILE)


KINDS = {
    kind.name: kind
    for kind in (
        Kind("step", pushes=push_rules.STEP, container=True, directory=False),
        Kind("plan", pushes=push_rules.PLAN, waits=True, parks=True, builds=True),
        Kind(
            "review",
            "run {run} is a review run, which reads only: it records what it finds with `evo-agents worker finding` "
            "and `evo-agents worker propose`, not `evo-agents worker {command}`, which only the agent of {kinds} uses",
            push_rules.NOTHING,
        ),
        Kind(
            "judge",
            "run {run} is a judge run, which reads and judges only: its verdict is the JSON object that ends the "
            "agent's last message, not `evo-agents worker {command}`, which only the agent of {kinds} uses",
            push_rules.NOTHING,
        ),
        Kind(
            "author",
            "run {run} is an author run, which writes a plan and changes nothing: it does not use `evo-agents worker "
            "{command}`",
            push_rules.NOTHING,
            answered=False,
            asks_in_chat=True,
            parks=True,
        ),
        Kind("chat", pushes=push_rules.CHAT, answered=False, verbatim=True, talks=True, parks=True),
        Kind("task", pushes=push_rules.TASK, container=True, answered=False, verbatim=True, hub_server=True),
        Kind(
            "replay",
            "run {run} is a replay run, which does a step of the project's history again and reports to no plan: "
            "leave your work in the directory and end your turn; the worker runs the step's verify, not `evo-agents "
            "worker {command}`, which only the agent of {kinds} uses",
            push_rules.NOTHING,
        ),
        Kind(
            "history",
            "run {run} is a history run, whose agent answers each batch it is handed with the JSON object its prompt "
            "asks for and uses no command: `evo-agents worker {command}` is for the agent of {kinds}",
            push_rules.NOTHING,
        ),
        Kind(
            "probe",
            "run {run} is a probe run, which does one task and reports to no plan: leave your work in the directory "
            "and end your turn; the worker checks it, not `evo-agents worker {command}`, which only the agent of "
            "{kinds} uses",
            push_rules.NOTHING,
        ),
        Kind(
            "release",
            "run {run} is a release run, whose agent uses no command: it commits on the run's branch and writes the "
            "version in {result}, and the worker pushes release/<version>, not `evo-agents worker {command}`, which "
            "only the agent of {kinds} uses",
            push_rules.RELEASE,
            answered=False,
            guarded=True,
        ),
        Kind(
            "question",
            'run {run} is a question run, which reads only and asks nobody: its answer is the "answer" it writes in '
            "{result}, not `evo-agents worker {command}`, which only the agent of {kinds} uses",
            push_rules.NOTHING,
            answered=False,
            verbatim=True,
            hub_server=True,
        ),
        Kind("browser", directory=False),
        Kind("command"),
        Kind(
            "evolve",
            "run {run} is an evolve run, whose agent rewrites the files of its folder and ends its turn: the worker "
            "measures and keeps them, not `evo-agents worker {command}`, which only the agent of {kinds} uses",
        ),
    )
}
CURATOR = "curator"  # not a kind of run: what a run of the Curator pushes as (``gitops.check_push``)
CONTAINER_KINDS = tuple(name for name, kind in KINDS.items() if kind.container)


def of(name: str | None) -> Kind:
    """The kind named ``name``; a run of one step's for a name this worker does not know, as ``runner.kinds``
    runs it."""
    return KINDS.get(name or "step") or KINDS["step"]


def push_rule(name: str) -> PushRule:
    """What a run of kind ``name`` pushes; for CURATOR, a Builder's or an evolve run's."""
    if name == CURATOR:
        return push_rules.CURATOR
    found = KINDS.get(name)
    return push_rules.OTHER if found is None else found.pushes


def kind_refusal(run_id: int, kind: str, kinds: tuple[str, ...], command: str) -> str:
    """Why `evo-agents worker COMMAND`, which only the agent of a run of ``kinds`` uses, refuses run ``run_id`` of
    ``kind``."""
    if kinds in (("review",), ("author",)):
        return (
            f"run {run_id} is {_article(kind)} {kind} run: only the agent of {_article(kinds[0])} {kinds[0]} run uses "
            f"`evo-agents worker {command}`"
        )
    return of(kind).refusal(run_id, command, kinds)
