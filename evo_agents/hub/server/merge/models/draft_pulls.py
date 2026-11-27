"""The draft pull requests of run branches (``service.draft_pulls``): what opening one made, a draft kept on a run,
what GitHub says of it, and the limits of the pass that settles them."""

from __future__ import annotations

from dataclasses import dataclass, field

KINDS = ("chat", "task")  # the kinds of run whose branch gets a draft pull request
WATCH_DAYS = 30  # a draft kept on a run queued longer ago is read no more
CHECK_MINUTES = 10  # a draft is read at most this often
SETTLE_MINUTES = 30  # a trace whose last run ended this long ago, none still to end, has ended
SETTLE_BATCH = 20  # drafts one pass reads
MAX_LINKED = 5  # pull requests of a trace's messages one draft reads
MAX_PULLS = 50  # the pull requests a run keeps (runs.pulls), one for each repo of its project at most


@dataclass(frozen=True)
class Made:
    """What ``open_for`` did with the push of a branch: the pull request opened (``opened``) or found, the base, and
    when it opened none, why (``nothing``) and the one of the branch it closed for that (``closed``)."""

    pull: dict | None
    opened: bool
    base: str | None
    nothing: str | None = None
    closed: dict | None = None


@dataclass(frozen=True)
class Draft:
    """A draft kept on a run: the run, its project, kind and branch, and the pull request (repo of the project, owner
    and name on GitHub, number, base, url) with what the hub noted of it."""

    run_id: int
    project_id: int
    branch: str
    repo: str
    owner: str
    name: str
    number: int
    base: str | None
    url: str
    noted: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """What GitHub says of a draft: ``merged``, ``closed``, ``close`` (with the comment and the pull request that
    replaced it) or ``open`` (its base, head and paths)."""

    state: str
    comment: str | None = None
    replaced_by: int | None = None
    base: str | None = None
    head: str | None = None
    paths: tuple[str, ...] = ()
