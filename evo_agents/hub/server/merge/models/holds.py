"""The keys and the bounds of the hold of a merge check for the project's triage (``service.holds``)."""

from __future__ import annotations

from datetime import timedelta

KEY = "triage"  # the key of a check's verdict that holds its hold and its thread
ACTIONS = REJUDGE, RETURN = ("rejudge", "return")
ROUND_KEYS = ("first", "second", "paths", "hidden_count")  # what a verdict holds of the judging of this head
REASON_CHARS = 1200
ASKS = 4  # mentions of the agent a hold makes at most while none made a chat run
ASK_EVERY = timedelta(minutes=10)
SKILL = "evo-hub:triaging-failed-runs"
COMMAND = "evo-agents worker triage merge-check"
