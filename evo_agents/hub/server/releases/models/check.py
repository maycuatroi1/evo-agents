"""What a check of a release schedule records (``service.check``)."""

from __future__ import annotations

from dataclasses import dataclass

NO_APP = "this hub has no GitHub App (EVO_HUB_GITHUB_APP_ID, EVO_HUB_GITHUB_APP_PRIVATE_KEY): it reads no tag"


@dataclass(frozen=True)
class Checked:
    """A check recorded: its id and outcome, and the cycle and run it opened."""

    id: int
    outcome: str
    cycle_id: int | None = None
    run_id: int | None = None
