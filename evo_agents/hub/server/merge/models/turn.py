"""The bounds of the turn of the hub's merge queue on GitLab, and the words of GitLab it reads (``service.turn``)."""

from __future__ import annotations

MAX_MOVES = 20  # moves of the turn of one target branch in one pass
RETRYABLE = frozenset({"failed", "canceled"})  # a pipeline the turn runs again once
NEEDS_REBASE = "need_rebase"  # GitLab's detailed_merge_status of a merge request behind its fast-forward target
CI_RUNNING = "ci_still_running"  # ... of one GitLab merges only once the pipeline of its head ends
