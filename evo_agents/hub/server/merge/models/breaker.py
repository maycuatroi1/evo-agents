"""The notice and the lines of the ledger the breaker of a repo of the hub's merge queue reads (``service.breaker``,
``repository.breaker``)."""

from __future__ import annotations

NOTICE = "merge_paused"  # runs.NOTICE_KINDS
MAX_FAILURES = 20  # lines of the ledger that tripped it, named in its reason
BLOCKING = ("gate", "pipeline", "rebased", "merged")  # the lines of a revert's merge request blocked
MISSED = ("revert_opened", "rollback")  # a merge the Judge passed, then found the culprit or rolled back
