"""The texts of a release cycle's moves (``service.cycles``): the audit action of a run it queues, the options of the
decision a stop asks its owner and why closing is recommended, and the web page of a project's release."""

from __future__ import annotations

DISPATCH = "release.dispatch"  # a cycle's run queued: "<project> cycle:<id> run:<id> mode=<mode>"
QUESTION_REASON_CHARS = 600
# Why the hub recommends closing a stopped cycle (``decision_asks``): it spends nothing and keeps the schedule.
CLOSE_REASON = (
    "Closing spends nothing more and keeps the schedule: its next daily check starts a new cycle when the newest tag "
    "is red or a packaging path changed. Go on only when one more run can get past the reason above."
)
OPTIONS = [
    {"key": "continue", "label": "Go on"},
    {"key": "close", "label": "Close the cycle", "recommended": True},
    {"key": "pause", "label": "Pause the release schedule"},
]


def releases_link(project: str) -> str:
    """The web page of a project's daily release."""
    return f"/p/{project}/releases"
