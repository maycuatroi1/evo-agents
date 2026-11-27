"""The values and bounds of the questions a core plugin asks its own agent types (API 1.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from evo_agents.hub import question_runs

ANSWER_EVENT: str = question_runs.ANSWER_EVENT
MAX_QUESTION_BYTES: int = question_runs.MAX_QUESTION_BYTES
MAX_ASKER_CHARS: int = question_runs.MAX_ASKER_CHARS
MAX_REF_CHARS: int = question_runs.MAX_REF_CHARS
MAX_ANSWER_CHARS: int = question_runs.MAX_ANSWER_CHARS


@dataclass(frozen=True)
class AgentQuestion:
    """A question of the plugin to its agent type ``agent_type`` of ``project``, its run ``run_id``; ``made`` is False
    when ``ref`` named this question before and nothing was asked now."""

    question_id: int
    run_id: int
    project: str
    agent_type: str
    ref: str
    made: bool


@dataclass(frozen=True)
class AgentAnswer:
    """What the plugin reads of a question: ``pending`` until its run ended, then done (``text`` the answer), failed
    (``failure_cause`` why), cancelled, or refused when the project is kept from the place the answer goes, read again
    each time; ``label`` is the project's default label, which the answer carries."""

    question_id: int
    ref: str
    project: str
    agent_type: str
    run_id: int
    state: Literal["pending", "done", "failed", "cancelled", "refused"]
    text: str | None
    label: Mapping[str, Any]
    failure_cause: str | None
