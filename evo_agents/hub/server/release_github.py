"""What the check of a release schedule reads of GitHub, with the workers' App (evo-agents-workers): the tags and the
target ref of the schedule's repo, then the files of the two commits it compares (``evo_agents.hub.releases``).

One token of the App's installation on the repo, for that repo alone and with contents read (``pulls.with_token``, use
``tree``), serves every read of a check and is revoked after it. The refs come from one POST of the command ls-refs to
``<github_url>/<owner>/<repo>.git/git-upload-pack``, git's own HTTP protocol as ``evo_agents.hub.nightly`` speaks it for
the tag nightly, with the token as the password of ``x-access-token`` so a private repo answers too: the tags under
the pattern's prefix and the target ref, each as the commit an annotated tag peels to. The trees of the newest tag's
commit and of the target's are read whole (``pulls.tree``); a tree GitHub cuts is never compared in part.

GitHub failing or refusing becomes ``ReadFailed``, whose text is safe to show and becomes the reason of the check.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from evo_agents import __version__
from evo_agents.hub import releases
from evo_agents.hub.nightly import DELIM, FLUSH, pkt_line, pkt_lines
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp

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
    """What a check read: the newest tag the pattern matches and its commit, the commit of the target ref, and the
    packaging paths that changed between the two."""

    base_tag: str
    base_commit: str
    target_commit: str
    changed: list[str] = field(default_factory=list)


def ls_refs(prefixes: list[str]) -> bytes:
    """The body of one ls-refs: the refs under ``prefixes``, each with the commit an annotated tag peels to."""
    lines = [pkt_line(f"ref-prefix {prefix}\n") for prefix in prefixes]
    return pkt_line("command=ls-refs\n") + DELIM + pkt_line("peel\n") + b"".join(lines) + FLUSH


def parse_refs(body: bytes) -> dict[str, str]:
    """{ref: commit} of an answer to ``ls_refs``: what an annotated tag peels to, else what the ref names."""
    found = {}
    for line in pkt_lines(body):
        oid, _, rest = line.partition(" ")
        ref, *attributes = rest.split(" ")
        peeled = [value.removeprefix("peeled:") for value in attributes if value.startswith("peeled:")]
        commit = peeled[0] if peeled else oid
        if ref and releases.OBJECT_NAME.match(commit):
            found[ref] = commit
    return found


async def read_refs(app: GitHubApp, token: str, owner: str, repo: str, prefixes: list[str]) -> dict[str, str]:
    """{ref: commit} of the refs of owner/repo under ``prefixes``."""
    doing = f"listing the refs of {owner}/{repo}"
    secret = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    url = f"{app.config.github_url}/{owner}/{repo}.git/git-upload-pack"
    headers = {**HEADERS, "Authorization": f"Basic {secret}"}
    response = await app._call("POST", url, doing, content=ls_refs(prefixes), headers=headers)
    if response.status_code != 200:
        raise ReadFailed(
            f"GitHub answered {response.status_code} while {doing}: the App's installation may not read its contents"
        )
    try:
        return parse_refs(response.content)
    except (ValueError, UnicodeDecodeError):
        raise ReadFailed(f"GitHub answered something other than git's ls-refs while {doing}") from None


def _target(refs: dict[str, str], target_ref: str) -> str | None:
    return next((refs[name] for name in releases.target_refs(target_ref) if name in refs), None)


async def _tree(app: GitHubApp, token: str, owner: str, repo: str, commit: str) -> dict[str, str]:
    files = await pulls.tree(app, token, owner, repo, commit)
    if files is None:
        raise ReadFailed(f"GitHub cut the tree of {releases.short(commit)} of {owner}/{repo}: it is not compared")
    return files


async def _read(app: GitHubApp, token: str, owner: str, repo: str, schedule) -> Reading:
    pattern, target_ref = schedule.tag_pattern, schedule.target_ref
    refs = await read_refs(
        app, token, owner, repo, [TAGS + releases.tag_prefix(pattern), *releases.target_refs(target_ref)]
    )
    tags = {ref.removeprefix(TAGS): commit for ref, commit in refs.items() if ref.startswith(TAGS)}
    newest = releases.latest_tag(tags, pattern)
    if newest is None:
        raise ReadFailed(
            f"no tag of {owner}/{repo} matches {pattern} with a version X.Y.Z: tag the first release by hand"
        )
    target = _target(refs, target_ref)
    if target is None:
        raise ReadFailed(f"{owner}/{repo} has no tag or branch {target_ref}")
    base_tag, base_commit = newest
    if base_commit == target:
        return Reading(base_tag, base_commit, target)
    base = await _tree(app, token, owner, repo, base_commit)
    head = await _tree(app, token, owner, repo, target)
    return Reading(base_tag, base_commit, target, releases.changed_paths(base, head, list(schedule.paths)))


async def read_release(app: GitHubApp, owner: str, repo: str, schedule) -> Reading:
    """What the check of ``schedule`` (its tag_pattern, target_ref and paths) reads of owner/repo; ``ReadFailed`` says
    why it could not."""
    try:
        return await pulls.with_token(app, owner, repo, "tree", lambda token: _read(app, token, owner, repo, schedule))
    except (GitHubRefused, GitHubUnavailable) as exc:
        raise ReadFailed(str(exc)) from None
