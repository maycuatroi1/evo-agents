"""The kinds of run this worker runs, by the claim's kind (``runs.RUN_KINDS``). A Run (``context``) composes the one
its claim names; no kind inherits from another or from the Run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from evo_agents.worker.runner.author import AuthorRun
from evo_agents.worker.runner.chat import ChatRun
from evo_agents.worker.runner.history import HistoryRun
from evo_agents.worker.runner.judge import JudgeRun
from evo_agents.worker.runner.plan import PlanRun
from evo_agents.worker.runner.release import ReleaseRun
from evo_agents.worker.runner.replay import ReplayRun
from evo_agents.worker.runner.review import ReviewRun
from evo_agents.worker.runner.step import StepRun
from evo_agents.worker.runner.task import TaskRun

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import RunKind

KINDS = {
    "step": StepRun,
    "plan": PlanRun,
    "review": ReviewRun,
    "judge": JudgeRun,
    "author": AuthorRun,
    "chat": ChatRun,
    "task": TaskRun,
    "replay": ReplayRun,
    "history": HistoryRun,
    "release": ReleaseRun,
}


def kind_class(spec: dict) -> type[RunKind]:
    """The kind of the run ``spec`` claims: StepRun for kind step, and for a claim that names no kind this worker
    knows."""
    return KINDS.get(spec.get("kind"), StepRun)
