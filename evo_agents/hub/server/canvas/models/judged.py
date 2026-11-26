"""What @strategist judges, as values: an item put before a judging run (``Item``), a row of canvas_judgings with its
verdict (``JudgingRow``), and the limits of one run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from evo_agents.hub.tables_canvas import MAX_JUDGED_TEXT_CHARS

MAX_ITEMS = 50  # put before one judging run
MAX_PASS_CHARS = 40_000  # of the text of the items of one judging run, so canvas__judge answers in one read
MAX_TRIES = 2  # judging runs that may end without judging an item before it is left out
FEEDBACK_KEY = "feedback"  # signal_keys.kind of the keys whose signals are judged


@dataclass(frozen=True)
class Item:
    """An item to put before a judging run: a piece of feedback (``signal``) or a claim's evidence."""

    claim_id: int
    source: str
    kind: str
    text: str
    synthetic: bool
    rests_on_run: bool
    label: dict
    measured_at: datetime
    key_id: int | None = None
    signal_id: int | None = None
    evidence_id: int | None = None

    def row(self, canvas_id: int, run_id: int) -> dict:
        return {**asdict(self), "text": self.text[:MAX_JUDGED_TEXT_CHARS], "canvas_id": canvas_id, "run_id": run_id}


@dataclass(frozen=True)
class JudgingRow:
    """An item a judging run had for a claim, and its verdict once judged."""

    id: int
    run_id: int | None
    source: str
    kind: str
    signal_id: int | None
    evidence_id: int | None
    text: str
    synthetic: bool
    rests_on_run: bool
    label: dict
    measured_at: datetime
    created_at: datetime
    verdict: str | None
    reason: str | None
    judged_at: datetime | None
    verdict_evidence_id: int | None
