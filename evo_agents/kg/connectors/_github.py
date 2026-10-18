"""Pull requests of a repo on GitHub, for the git connector: number, title, state, branches and merge commit.

A run asks ``GET /repos/{owner}/{repo}/pulls`` for every state, the most recently updated first, PAGE at a time, and
stops at the first pull request updated before ``since`` (the newest ``updated_at`` the run before it saw), or after
MAX_PAGES. Only those fields are kept: the body and the comments of a pull request, its free text, are never read.

The token comes from the source's credentials (GH_TOKEN or GITHUB_TOKEN in its environment), else this process's
environment, else ``gh auth token``; without one no pull request is read. The token only ever goes into the
Authorization header of a request to API_URL: an error names the repo and GitHub's status, never the request's
headers, and a redirect is refused rather than followed with the header to another host. EVO_KG_PULLS=0 turns the
reading off for the machine (the test suite sets it, so no test asks GitHub).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from evo_agents import __version__

API_URL = "https://api.github.com"
PAGE = 100
MAX_PAGES = 30  # the 3,000 most recently updated pull requests on a first read
TIMEOUT = 30.0
GH_TIMEOUT = 10.0
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")
SWITCH = "EVO_KG_PULLS"


class PullsError(RuntimeError):
    """GitHub could not be read; the message names the repo and the reason, never a credential."""


@dataclass
class Pulls:
    """What one read found: the pull requests, the newest ``updated_at`` among them, and whether it stopped at
    MAX_PAGES before reaching ``since``."""

    pulls: list[dict] = field(default_factory=list)
    newest: str | None = None
    cut: bool = False


def enabled(env: dict | None = None) -> bool:
    value = (env or {}).get(SWITCH) or os.environ.get(SWITCH, "")
    return value.strip().lower() not in ("0", "false", "no", "off")


def token(env: dict | None = None) -> str | None:
    """A GitHub token: the source's credentials, this process's environment, then the GitHub CLI's sign-in."""
    for name in TOKEN_VARIABLES:
        value = (env or {}).get(name) or os.environ.get(name)
        if value:
            return value.strip()
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        done = subprocess.run(
            [gh, "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the Authorization header must not follow a redirect to another host


_OPENER = urllib.request.build_opener(_NoRedirect)


def _page(project: str, page: int, secret: str) -> list[dict]:
    query = urllib.parse.urlencode(
        {"state": "all", "sort": "updated", "direction": "desc", "per_page": PAGE, "page": page}
    )
    request = urllib.request.Request(
        f"{API_URL}/repos/{project}/pulls?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {secret}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"evo-agents/{__version__}",
        },
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PullsError(f"GitHub answered HTTP {exc.code} for the pull requests of {project}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise PullsError(f"GitHub could not be read for the pull requests of {project}: {reason}") from None
    if not isinstance(payload, list):
        raise PullsError(f"GitHub did not answer with a list of pull requests of {project}")
    return payload


def shape(raw: dict) -> dict | None:
    """The fields of a pull request the graph keeps; None for an entry that is not one."""
    number, updated = raw.get("number"), raw.get("updated_at")
    if type(number) is not int or not isinstance(updated, str):
        return None
    head, base = raw.get("head") or {}, raw.get("base") or {}
    merged = isinstance(raw.get("merged_at"), str)
    merge_commit = raw.get("merge_commit_sha") if merged else None  # an open one's is GitHub's trial merge
    return {
        "number": number,
        "title": str(raw.get("title") or ""),
        "status": "merged" if merged else str(raw.get("state") or "open"),
        "head": head.get("ref") if isinstance(head.get("ref"), str) else None,
        "base": base.get("ref") if isinstance(base.get("ref"), str) else None,
        "merge_commit": merge_commit if isinstance(merge_commit, str) else None,
        "url": raw.get("html_url") if isinstance(raw.get("html_url"), str) else None,
        "created": raw.get("created_at") if isinstance(raw.get("created_at"), str) else None,
        "updated": updated,
    }


def pulls(project: str, secret: str, since: str | None = None) -> Pulls:
    """The pull requests of ``project`` (``owner/repo``) updated at or after ``since``, newest first."""
    found = Pulls()
    for page in range(1, MAX_PAGES + 1):
        raw = _page(project, page, secret)
        for entry in raw:
            pull = shape(entry) if isinstance(entry, dict) else None
            if pull is None:
                continue
            if since and pull["updated"] < since:
                return found
            found.pulls.append(pull)
            found.newest = max(found.newest or pull["updated"], pull["updated"])
        if len(raw) < PAGE:
            return found
    found.cut = True
    return found
