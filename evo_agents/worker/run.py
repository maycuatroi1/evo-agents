"""One run on this worker, from the claim to its last report: the names the daemon and the tests import.

The daemon holds a ``Run`` (``runner.context``) for each run it claimed: its state and log, what the heartbeat asks of
it, and the parts every kind of run shares. The run's kind is composed into it, never inherited
(``runner.kinds``): each kind has a module of its own, which says what the kind does from the claim to its end.

- ``runner.step``, ``StepRun``: a run of one step: a worktree on the plan's branch, the agent, its verify commands run
  again, the branch pushed.
- ``runner.plan``, ``PlanRun``: every step of a plan not done yet, in one session, over a directory with a worktree
  of each repo; it waits for its owner's answers between turns, and the hub may park it.
- ``runner.review``, ``ReviewRun``: the Curator's Reviewer of one night of a project, reading only.
- ``runner.judge``, ``JudgeRun``: the Curator's Judge of one change, whose code runs as code the worker does not trust.
- ``runner.author``, ``AuthorRun``: an execution plan written from its owner's request, in the run's chat.

The parts they share, each a module of ``runner``: ``sender`` (the events on their way to the hub), ``reports`` (the
moves of the run and the blobs of its log and diff), ``agent`` and ``terminal`` (the agent, headless or in its
terminal UI), ``inbox`` (the owner's messages), ``access`` (the leases and the preflight), ``origin`` (fetching and
the branch a worktree works on), ``verify`` (the result file and the verify commands), ``push`` (the commit and the
owner's notices), ``watchdog`` (the budget and the watchdog of a run of the Curator), ``directory`` (the directory of
a run over several repos) and ``conversation`` (the turns of its agent and the waits for its owner). The decisions
that need no I/O are pure functions in ``runner.transitions``.
"""

from __future__ import annotations

from evo_agents.worker.runner.agent import HANDBACK_PROMPT
from evo_agents.worker.runner.author import AuthorRun
from evo_agents.worker.runner.common import Parked, ReportRefused, RunFailed, RunGone, Stopped
from evo_agents.worker.runner.context import Run, RunKind
from evo_agents.worker.runner.conversation import ANSWER_PROMPT, RESUME_PROMPT, read_asked
from evo_agents.worker.runner.judge import JudgeRun
from evo_agents.worker.runner.kinds import KINDS, kind_class
from evo_agents.worker.runner.plan import PlanRun
from evo_agents.worker.runner.review import WORKTREE_FIGURES, ReviewRun
from evo_agents.worker.runner.sender import Sender
from evo_agents.worker.runner.step import StepRun

__all__ = [
    "ANSWER_PROMPT",
    "HANDBACK_PROMPT",
    "KINDS",
    "RESUME_PROMPT",
    "WORKTREE_FIGURES",
    "AuthorRun",
    "JudgeRun",
    "Parked",
    "PlanRun",
    "ReportRefused",
    "ReviewRun",
    "Run",
    "RunFailed",
    "RunGone",
    "RunKind",
    "Sender",
    "StepRun",
    "Stopped",
    "kind_class",
    "read_asked",
    "run_class",
]


def run_class(spec: dict) -> type[Run]:
    """The class of the run ``spec`` claims: Run, for every kind; the kind it composes (``kind_class``: StepRun,
    PlanRun, ReviewRun, JudgeRun or AuthorRun) brings what differs."""
    return Run
