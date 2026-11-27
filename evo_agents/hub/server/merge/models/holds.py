"""The keys, the bounds and the frame of the hold of a merge check for the project's triage (``service.holds``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evo_agents.hub.server.chats.service.agents import AgentRun
    from evo_agents.hub.server.security import Principal

KEY = "triage"  # the key of a check's verdict that holds its hold and its thread
ACTIONS = REJUDGE, RETURN = ("rejudge", "return")
ROUND_KEYS = ("first", "second", "paths", "hidden_count")  # what a verdict holds of the judging of this head
REASON_CHARS = 1200
ASKS = 4  # mentions of the agent a hold makes at most while none made a chat run
ASK_EVERY = timedelta(minutes=10)
SKILL = "evo-hub:triaging-failed-runs"
COMMAND = "evo-agents worker triage merge-check"


@dataclass(frozen=True)
class Frame:
    """What the agent of a held check's thread acts on: its chat run, the thread, the check and the owner."""

    agent: AgentRun
    chat_run_id: int
    chat_id: int
    root_id: int
    row: Any  # the check's row, locked
    owner: Principal
