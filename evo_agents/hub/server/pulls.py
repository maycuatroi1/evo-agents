"""The hub's own calls to GitHub for the Curator's changes: pull requests, their files, CI on their head, the Judge's
check run and the merge, with the workers' App (evo-agents-workers); and the check of a repo's ruleset, with the
Curator's App (evo-agents-curator). ``evo_agents.hub.server.changes`` decides what to call when.

Each call asks the App's installation on the repo for a token of that repo alone, with the permissions the call needs
and no more (SERVER_PERMISSIONS: open a pull request with pull_requests write; read one, its files and its CI with
pull_requests, checks and statuses read; write the Judge's check run with checks write; merge with contents and
pull_requests write), uses it, and revokes it. The tokens a worker's run gets keep ``GITHUB_PERMISSIONS`` as they were.
Calls go through ``GitHubClient`` (a timeout, no redirect, one log line without the token): GitHub failing raises
``GitHubUnavailable``, which the job takes as "try again next pass"; GitHub refusing raises ``GitHubRefused`` with a
message safe to show.

``check_ruleset`` answers whether the Curator's App is kept off a repo's default branch: GET /repos/{o}/{r} names the
default branch, GET /repos/{o}/{r}/rules/branches/{branch} the rules that apply to it, and for each ruleset with a rule
of type ``update`` (only its bypass actors may update the branch), GET /repos/{o}/{r}/rulesets/{id} must say it is
``active`` and, asked with the Curator's own token, ``current_user_can_bypass`` ``never``, and must not list the
Curator's App among its bypass actors when it shows them. Anything else, a field missing included, is not protected.
It also names the checks the active rulesets of the branch require (``required_checks``), which a merge needs.

A list GitHub pages through ends at MAX_PAGES: one longer than that is ``Truncated`` (a ``GitHubRefused``), never read
in part, so a pull request of more files than the hub reads, or of more check runs, fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.parse import quote

from evo_agents.hub.judge import JUDGE_CHECK_NAME, RULE_REQUIRED_CHECKS, RULE_UPDATE, required_checks
from evo_agents.hub.server.github import API_HEADERS, GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, not_installed

log = logging.getLogger(__name__)

T = TypeVar("T")

SERVER_PERMISSIONS = {
    "open": {"pull_requests": "write", "metadata": "read"},
    "read": {"pull_requests": "read", "checks": "read", "statuses": "read", "metadata": "read"},
    "check": {"checks": "write", "metadata": "read"},
    "merge": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    "ruleset": {"metadata": "read"},
}
PAGE = 100
MAX_PAGES = 30  # GitHub lists at most 3000 files of a pull request
MERGE_METHOD = "merge"  # a merge commit, which `git revert -m 1` undoes


class Truncated(GitHubRefused):
    """GitHub lists more than the hub reads: the list is not read in part."""


@dataclass(frozen=True)
class RulesetCheck:
    """What ``check_ruleset`` found of a repo: whether its default branch keeps the Curator's App off, and why; and the
    checks its active rulesets require before a merge."""

    protected: bool
    default_branch: str | None
    reason: str
    rulesets: list[dict] = field(default_factory=list)  # each {id, enforcement, can_bypass}
    required_checks: list[dict] = field(default_factory=list)  # each {context, integration_id}


def _path(*parts: str | int) -> str:
    return "/".join(quote(str(part), safe="") for part in parts)


async def with_token(app: GitHubApp, owner: str, repo: str, use: str, call: Callable[[str], Awaitable[T]]) -> T:
    """Run ``call(token)`` with a token of ``app``'s installation on owner/repo for that repo alone and the
    permissions of SERVER_PERMISSIONS[use], then revoke it. ``GitHubRefused`` when the App is not installed there."""
    installation = await app.installation(owner, repo)
    if installation is None:
        raise GitHubRefused(not_installed(owner, repo))
    made = await app.create_token(installation, [repo], SERVER_PERMISSIONS[use])
    try:
        return await call(made.token)
    finally:
        try:
            await app.revoke(made.token)
        except GitHubUnavailable as exc:
            log.warning("a server token of the hub is left to expire", extra={"why": str(exc)})


async def _api(app: GitHubApp, token: str, method: str, path: str, doing: str, **kwargs):
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
    return await app._call(method, f"{app.config.github_api_url}/{path}", doing, headers=headers, **kwargs)


def _refused(response, doing: str) -> GitHubRefused:
    message = ""
    try:
        data = response.json()
        message = data.get("message") if isinstance(data, dict) and isinstance(data.get("message"), str) else ""
    except ValueError:
        pass
    shown = " ".join(message.split())[:200]
    return GitHubRefused(f"GitHub answered {response.status_code} while {doing}" + (f": {shown}" if shown else ""))


async def repository(app: GitHubApp, token: str, owner: str, repo: str) -> dict:
    doing = f"reading {owner}/{repo}"
    response = await _api(app, token, "GET", _path("repos", owner, repo), doing)
    if response.status_code != 200:
        raise _refused(response, doing)
    return app._json(response, doing)


async def find_pull(app: GitHubApp, token: str, owner: str, repo: str, branch: str) -> dict | None:
    """The open pull request of ``branch`` (of owner/repo itself), or None."""
    doing = f"looking for the pull request of {branch} on {owner}/{repo}"
    params = {"head": f"{owner}:{branch}", "state": "open", "per_page": 1}
    response = await _api(app, token, "GET", _path("repos", owner, repo, "pulls"), doing, params=params)
    if response.status_code != 200:
        raise _refused(response, doing)
    items = response.json()
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None


async def open_pull(
    app: GitHubApp, token: str, owner: str, repo: str, *, branch: str, base: str, title: str, body: str
) -> dict:
    """The pull request of ``branch`` into ``base``: opened now, or the one open already."""
    doing = f"opening a pull request of {branch} on {owner}/{repo}"
    payload = {"title": title, "head": branch, "base": base, "body": body, "maintainer_can_modify": False}
    response = await _api(app, token, "POST", _path("repos", owner, repo, "pulls"), doing, json=payload)
    if response.status_code == 201:
        return app._json(response, doing)
    if response.status_code == 422:
        found = await find_pull(app, token, owner, repo, branch)
        if found is not None:
            return found
    raise _refused(response, doing)


async def pull(app: GitHubApp, token: str, owner: str, repo: str, number: int) -> dict:
    doing = f"reading pull request #{number} of {owner}/{repo}"
    response = await _api(app, token, "GET", _path("repos", owner, repo, "pulls", number), doing)
    if response.status_code != 200:
        raise _refused(response, doing)
    return app._json(response, doing)


async def _pages(app: GitHubApp, token: str, path: str, doing: str, key: str | None = None) -> list[dict]:
    found: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = await _api(app, token, "GET", path, doing, params={"per_page": PAGE, "page": page})
        if response.status_code != 200:
            raise _refused(response, doing)
        data = response.json()
        items = data.get(key) if key and isinstance(data, dict) else data
        if not isinstance(items, list):
            raise GitHubUnavailable(f"GitHub answered without a list while {doing}")
        found += [item for item in items if isinstance(item, dict)]
        if len(items) < PAGE:
            return found
    raise Truncated(f"GitHub lists more than {MAX_PAGES * PAGE} items while {doing}: the hub does not read a part")


async def pull_files(app: GitHubApp, token: str, owner: str, repo: str, number: int) -> list[dict]:
    doing = f"reading the files of pull request #{number} of {owner}/{repo}"
    return await _pages(app, token, _path("repos", owner, repo, "pulls", number, "files"), doing)


async def pull_commits(app: GitHubApp, token: str, owner: str, repo: str, number: int) -> list[dict]:
    """The commits of pull request ``number``, oldest first, as GitHub lists them (250 at most)."""
    doing = f"reading the commits of pull request #{number} of {owner}/{repo}"
    return await _pages(app, token, _path("repos", owner, repo, "pulls", number, "commits"), doing)


async def check_runs(app: GitHubApp, token: str, owner: str, repo: str, sha: str) -> list[dict] | None:
    """The check runs of commit ``sha``; None when GitHub refuses to show them."""
    doing = f"reading the check runs of {sha[:12]} on {owner}/{repo}"
    try:
        return await _pages(app, token, _path("repos", owner, repo, "commits", sha, "check-runs"), doing, "check_runs")
    except GitHubRefused as exc:
        log.info("check runs not read", extra={"why": str(exc)})
        return None


async def combined_status(app: GitHubApp, token: str, owner: str, repo: str, sha: str) -> dict | None:
    """The combined status of commit ``sha``; None when GitHub refuses to show it (the App lacks statuses read)."""
    doing = f"reading the commit statuses of {sha[:12]} on {owner}/{repo}"
    response = await _api(app, token, "GET", _path("repos", owner, repo, "commits", sha, "status"), doing)
    if response.status_code != 200:
        log.info("commit statuses not read", extra={"status": response.status_code})
        return None
    return app._json(response, doing)


async def create_check_run(
    app: GitHubApp,
    token: str,
    owner: str,
    repo: str,
    *,
    sha: str,
    conclusion: str,
    title: str,
    summary: str,
    external_id: str,
) -> int:
    """Write the Judge's check run on commit ``sha``, completed with ``conclusion``; its id."""
    doing = f"writing the Judge's check run on {sha[:12]} of {owner}/{repo}"
    payload = {
        "name": JUDGE_CHECK_NAME,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "external_id": external_id,
        "output": {"title": title, "summary": summary},
    }
    response = await _api(app, token, "POST", _path("repos", owner, repo, "check-runs"), doing, json=payload)
    if response.status_code != 201:
        raise _refused(response, doing)
    found = app._json(response, doing).get("id")
    if type(found) is not int:
        raise GitHubUnavailable(f"GitHub answered without the check run's id while {doing}")
    return found


async def merge_pull(app: GitHubApp, token: str, owner: str, repo: str, number: int, *, sha: str, title: str) -> str:
    """Merge pull request ``number`` when its head is ``sha`` still (GitHub refuses with 409 otherwise); the merge
    commit."""
    doing = f"merging pull request #{number} of {owner}/{repo}"
    payload = {"sha": sha, "merge_method": MERGE_METHOD, "commit_title": title}
    response = await _api(app, token, "PUT", _path("repos", owner, repo, "pulls", number, "merge"), doing, json=payload)
    if response.status_code != 200:
        raise _refused(response, doing)
    data = app._json(response, doing)
    merged = data.get("sha")
    if data.get("merged") is not True or not isinstance(merged, str):
        raise GitHubRefused(f"GitHub did not merge pull request #{number} of {owner}/{repo}")
    return merged


async def check_ruleset(curator_app: GitHubApp, owner: str, repo: str) -> RulesetCheck:
    """Whether the default branch of owner/repo keeps the Curator's App off (see the module's docstring), asked with
    a token of the Curator's App. ``GitHubUnavailable`` when GitHub fails; a refusal is a check that did not pass."""

    async def look(token: str) -> RulesetCheck:
        found = await repository(curator_app, token, owner, repo)
        branch = found.get("default_branch")
        if not isinstance(branch, str) or not branch:
            return RulesetCheck(False, None, f"GitHub names no default branch of {owner}/{repo}")
        doing = f"reading the rules of {branch} on {owner}/{repo}"
        path = _path("repos", owner, repo, "rules", "branches", branch)
        rules = await _pages(curator_app, token, path, doing)
        blocking = sorted({rule.get("ruleset_id") for rule in rules if rule.get("type") == RULE_UPDATE} - {None})
        asking = sorted({rule.get("ruleset_id") for rule in rules if rule.get("type") == RULE_REQUIRED_CHECKS} - {None})
        rulesets: dict = {}

        async def ruleset(ruleset_id) -> dict | None:
            if ruleset_id not in rulesets:
                doing = f"reading ruleset {ruleset_id} of {owner}/{repo}"
                response = await _api(
                    curator_app, token, "GET", _path("repos", owner, repo, "rulesets", ruleset_id), doing
                )
                rulesets[ruleset_id] = curator_app._json(response, doing) if response.status_code == 200 else None
            return rulesets[ruleset_id]

        active = []
        for ruleset_id in asking:  # the checks of a ruleset only evaluated are not required
            data = await ruleset(ruleset_id)
            if data is not None and data.get("enforcement") == "active":
                active.append(ruleset_id)
        required = required_checks([rule for rule in rules if rule.get("ruleset_id") in active])
        if not blocking:
            return RulesetCheck(
                False,
                branch,
                f"no ruleset restricts updates of {branch} on {owner}/{repo}: anyone who may push can",
                required_checks=required,
            )
        seen, keeping = [], False
        for ruleset_id in blocking:
            data = await ruleset(ruleset_id)
            if data is None:
                seen.append({"id": ruleset_id, "enforcement": None, "can_bypass": None})
                continue
            bypass = data.get("current_user_can_bypass")
            actors = data.get("bypass_actors") if isinstance(data.get("bypass_actors"), list) else []
            listed = curator_app.app_id is not None and any(
                isinstance(actor, dict)
                and actor.get("actor_type") == "Integration"
                and actor.get("actor_id") == curator_app.app_id
                for actor in actors
            )
            seen.append({"id": ruleset_id, "enforcement": data.get("enforcement"), "can_bypass": bypass})
            if data.get("enforcement") == "active" and bypass == "never" and not listed:
                keeping = True
        if keeping:
            return RulesetCheck(True, branch, f"an active ruleset keeps the Curator's App off {branch}", seen, required)
        return RulesetCheck(
            False,
            branch,
            f"no active ruleset of {branch} on {owner}/{repo} says the Curator's App may never bypass it",
            seen,
            required,
        )

    try:
        return await with_token(curator_app, owner, repo, "ruleset", look)
    except GitHubRefused as exc:
        return RulesetCheck(False, None, str(exc))
