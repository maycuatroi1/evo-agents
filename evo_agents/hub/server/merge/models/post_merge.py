"""The notice, the bounds, the states and the texts of what follows a merge of the hub's merge queue
(``service.post_merge``, ``service.breaker``, ``models.view``)."""

from __future__ import annotations

from evo_agents.hub import post_merge

NOTICE = "merge_default_branch"  # the Inbox kind of the queue's notices (runs.NOTICE_KINDS)
MAX_NOTE_CHARS = 2000  # of an admin's note on a sample (merge_samples.note)
PASSED = {"passed": True, "failed": False, "none": None}  # a check's result as merge_events.passed
AFTER_MERGE = (*post_merge.UAT_STATES, "releasing", "done")


def short(sha: str | None) -> str:
    return (sha or "an unknown commit")[:12]


def link(project: str) -> str:
    return f"/p/{project}/merge-queue"
