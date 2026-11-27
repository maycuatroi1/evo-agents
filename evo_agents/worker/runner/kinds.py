"""The kinds of run this worker runs, by the claim's kind (``runs.RUN_KINDS``), each a ``kind.RunKind`` listed here by
hand under the name its traits declare. A Run (``context``) composes the one its claim names."""

from __future__ import annotations

from evo_agents.worker.runner.author import AuthorRun
from evo_agents.worker.runner.browser_login import BrowserLoginRun
from evo_agents.worker.runner.chat import ChatRun
from evo_agents.worker.runner.command import CommandRun
from evo_agents.worker.runner.evolve import EvolveRun
from evo_agents.worker.runner.history import HistoryRun
from evo_agents.worker.runner.judge import JudgeRun
from evo_agents.worker.runner.kind import RunKind
from evo_agents.worker.runner.plan import PlanRun
from evo_agents.worker.runner.probe import ProbeRun
from evo_agents.worker.runner.question import QuestionRun
from evo_agents.worker.runner.release import ReleaseRun
from evo_agents.worker.runner.replay import ReplayRun
from evo_agents.worker.runner.review import ReviewRun
from evo_agents.worker.runner.step import StepRun
from evo_agents.worker.runner.task import TaskRun

KINDS: dict[str, type[RunKind]] = {
    kind.traits.name: kind
    for kind in (
        StepRun,
        PlanRun,
        ReviewRun,
        JudgeRun,
        AuthorRun,
        ChatRun,
        TaskRun,
        ReplayRun,
        HistoryRun,
        ProbeRun,
        ReleaseRun,
        QuestionRun,
        BrowserLoginRun,
        CommandRun,
        EvolveRun,
    )
}


def kind_class(spec: dict) -> type[RunKind]:
    """The kind of the run ``spec`` claims: StepRun for kind step, and for a claim that names no kind this worker
    knows."""
    return KINDS.get(spec.get("kind"), StepRun)
