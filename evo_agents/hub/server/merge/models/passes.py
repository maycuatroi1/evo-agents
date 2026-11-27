"""An admin's pass of a hold of the hub's merge queue, and its bounds (``service.passes``, ``repository.passes``,
``models.view``)."""

from __future__ import annotations

from dataclasses import dataclass

MAX_REASON_CHARS = 1000  # of an admin's reason for a pass, in the ledger's line
AUDIT_REASON_CHARS = 300  # of it in the audit's target
PERSON = "person"  # the key of the detail of a ledger line that is an admin's pass: their login


@dataclass(frozen=True)
class Pass:
    """An admin's pass of a hold that still holds: its ledger line, the head it was given at, who and why."""

    id: int
    sha: str | None
    login: str
    line: str
