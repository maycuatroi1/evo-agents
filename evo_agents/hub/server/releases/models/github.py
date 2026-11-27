"""What the check of a release schedule reads of GitHub (``service.github``): the reading, the failure to read, and
the headers of git's ls-refs."""

from __future__ import annotations

from dataclasses import dataclass, field

from evo_agents import __version__

TAGS = "refs/tags/"
HEADERS = {
    "Git-Protocol": "version=2",
    "Content-Type": "application/x-git-upload-pack-request",
    "Accept": "application/x-git-upload-pack-result",
    "User-Agent": f"git/evo-agents-hub-{__version__}",
}


class ReadFailed(Exception):
    """What the check could not read, in words safe to show."""


@dataclass(frozen=True)
class Reading:
    """What a check read: the newest tag the pattern matches and its commit, the commit of the target ref, the
    packaging paths that changed between the two, and the range of the release when one did."""

    base_tag: str
    base_commit: str
    target_commit: str
    changed: list[str] = field(default_factory=list)
    span: dict | None = None
