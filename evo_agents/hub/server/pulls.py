"""The hub's own calls to GitHub for the Curator's changes: pull requests, their files, CI on their head, the Judge's
check run and the merge, with the workers' App (evo-agents-workers); and the check of a repo's ruleset, with the
Curator's App (evo-agents-curator). ``evo_agents.hub.server.changes`` decides what to call when.

Each call asks the App's installation on the repo for a token of that repo alone, with the permissions the call needs
and no more (SERVER_PERMISSIONS: open a pull request with pull_requests write and contents read, so the token sees the
head and base branches of a private repo; read one, its files and its CI with pull_requests, checks and statuses read;
write the Judge's check run with checks write; merge with contents and pull_requests write; read a branch, a tree and
a comparison with contents read; read the runs, jobs and logs of a release tag's workflow with actions read, and run
its failed jobs again with actions write, ``evo_agents.hub.server.release_actions``), uses it, and revokes it.
The tokens a worker's run gets keep ``GITHUB_PERMISSIONS`` as they were. Calls go through ``GitHubClient`` (a timeout,
no redirect, one log line without the token): GitHub failing raises ``GitHubUnavailable``, which the job takes as "try
again next pass"; GitHub refusing raises ``Refused`` (a ``GitHubRefused``) with a message safe to show, which repeats
GitHub's ``message`` and the field, code and message of each item of its ``errors`` (``Refused.errors``).

``open_pull`` tries again, after HEAD_WAITS, while GitHub's errors say the head branch is not there (field ``head``),
as it may not be for a moment after the push; any other refusal is final.

For the measure of a knowledge graph's health (``evo_agents.hub.server.kg_health``) the workers' App reads, with
``contents: read``, the commit a default branch points at (``branch_head``), the files of a commit's tree
(``tree``), and how many commits of the branch a commit lacks (``compare``); for the range of a release
(``evo_agents.hub.server.release_github``), the commits a commit has that the newest release tag lacks
(``compare_commits``), and for landing it (``evo_agents.hub.server.release_land``), the tag made on its merge commit
(``create_ref``, with the merge's token); for the tier of a change of knowledge.yaml
(``evo_agents.hub.server.change_tiers``), a pull request's merge base (``compare``) and the text of a file at a commit
(``file_text``); for a global skill the hub publishes from a merge commit (``evo_agents.hub.server.skill_release``),
the files of the skill's directory at that commit, its tree and then each file's blob (``directory_files``).

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

import asyncio
import base64
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
    "open": {"pull_requests": "write", "contents": "read", "metadata": "read"},
    "read": {"pull_requests": "read", "checks": "read", "statuses": "read", "metadata": "read"},
    "check": {"checks": "write", "metadata": "read"},
    "merge": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    "ruleset": {"metadata": "read"},
    "tree": {"contents": "read", "metadata": "read"},
    "ci": {"actions": "read", "metadata": "read"},
    "rerun": {"actions": "write", "metadata": "read"},
}
PAGE = 100
MAX_PAGES = 30  # GitHub lists at most 3000 files of a pull request
MERGE_METHOD = "merge"  # a merge commit, which `git revert -m 1` undoes
HEAD_WAITS = (1.0, 3.0)  # seconds before each new try of a pull request whose head GitHub does not see yet
MAX_ERRORS = 5  # items of GitHub's errors a refusal repeats
MAX_SHOWN = 200  # characters of each text of GitHub's a refusal repeats
FILE_MODES = ("100644", "100755")  # a regular file and an executable one, as a git tree names them


class Refused(GitHubRefused):
    """GitHub answered a call with a refusal: its ``status`` and the items of its ``errors``, each {field, code,
    message} as far as GitHub gave them."""

    def __init__(self, text: str, status: int | None = None, errors: list[dict] | None = None):
        super().__init__(text)
        self.status = status
        self.errors = errors or []


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


def _shown(value) -> str:
    return " ".join(value.split())[:MAX_SHOWN] if isinstance(value, str) else ""


def _errors(data) -> list[dict]:
    """The items of GitHub's ``errors``, each {field, code, message} with what GitHub gave of them; an item that is
    text alone is its message."""
    found = data.get("errors") if isinstance(data, dict) else None
    items = []
    for item in found[:MAX_ERRORS] if isinstance(found, list) else ():
        item = item if isinstance(item, dict) else {"message": item}
        kept = {key: _shown(item.get(key)) for key in ("field", "code", "message")}
        if any(kept.values()):
            items.append({key: value for key, value in kept.items() if value})
    return items


def _said(errors: list[dict]) -> str:
    """GitHub's errors as a refusal says them: "field head, code invalid; code custom: Draft pull requests are ..."."""
    said = []
    for item in errors:
        where = ", ".join(f"{key} {item[key]}" for key in ("field", "code") if item.get(key))
        message = item.get("message", "")
        said.append(f"{where}: {message}" if where and message else where or message)
    return "; ".join(said)


def _refused(response, doing: str) -> Refused:
    try:
        data = response.json()
    except ValueError:
        data = None
    message = _shown(data.get("message")) if isinstance(data, dict) else ""
    errors = _errors(data)
    detail = _said(errors)
    text = f"GitHub answered {response.status_code} while {doing}" + (f": {message}" if message else "")
    if detail:
        text += f" ({detail})" if message else f": {detail}"
    return Refused(text, response.status_code, errors)


def head_missing(errors: list[dict]) -> bool:
    """Whether GitHub's errors say the head branch of a pull request is not there (yet): field head."""
    return any(item.get("field") == "head" for item in errors)


def drafts_refused(errors: list[dict]) -> bool:
    """Whether GitHub's errors say the repo takes no draft pull request: an item about drafts."""
    return any("draft" in f"{item.get('field', '')} {item.get('message', '')}".lower() for item in errors)


async def repository(app: GitHubApp, token: str, owner: str, repo: str) -> dict:
    doing = f"reading {owner}/{repo}"
    response = await _api(app, token, "GET", _path("repos", owner, repo), doing)
    if response.status_code != 200:
        raise _refused(response, doing)
    return app._json(response, doing)


async def branch_head(app: GitHubApp, token: str, owner: str, repo: str, branch: str) -> dict:
    """The commit ``branch`` of owner/repo points at: {sha, date}, its committer time."""
    doing = f"reading branch {branch} of {owner}/{repo}"
    response = await _api(app, token, "GET", _path("repos", owner, repo, "branches", branch), doing)
    if response.status_code != 200:
        raise _refused(response, doing)
    commit = app._json(response, doing).get("commit")
    commit = commit if isinstance(commit, dict) else {}
    sha = commit.get("sha")
    if not isinstance(sha, str):
        raise GitHubUnavailable(f"GitHub answered without the commit while {doing}")
    when = ((commit.get("commit") or {}).get("committer") or {}).get("date")
    return {"sha": sha, "date": when if isinstance(when, str) else None}


async def tree(app: GitHubApp, token: str, owner: str, repo: str, sha: str) -> dict[str, str] | None:
    """The files of commit ``sha`` of owner/repo, {path: blob SHA}, as GitHub lists its tree whole; None when GitHub
    cut the listing (a tree of more than 100,000 entries), which is never read in part."""
    doing = f"reading the tree of {sha[:12]} of {owner}/{repo}"
    path = _path("repos", owner, repo, "git", "trees", sha)
    response = await _api(app, token, "GET", path, doing, params={"recursive": "1"})
    if response.status_code != 200:
        raise _refused(response, doing)
    data = app._json(response, doing)
    if data.get("truncated") is True:
        return None
    entries = data.get("tree")
    if not isinstance(entries, list):
        raise GitHubUnavailable(f"GitHub answered without a tree while {doing}")
    return {
        item["path"]: item["sha"]
        for item in entries
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and isinstance(item.get("path"), str)
        and isinstance(item.get("sha"), str)
    }


async def compare(app: GitHubApp, token: str, owner: str, repo: str, base: str, head: str) -> dict:
    """How commit ``head`` of owner/repo stands to ``base``: GitHub's status (ahead, behind, identical, diverged), the
    commits ``head`` has that ``base`` lacks (``ahead_by``) and those ``base`` has that ``head`` lacks (``behind_by``),
    and the committer time of the oldest commit ``head`` has that ``base`` lacks (``oldest``), GitHub listing them
    oldest first."""
    doing = f"comparing {base[:12]} with {head[:12]} on {owner}/{repo}"
    path = _path("repos", owner, repo, "compare", f"{base}...{head}")
    response = await _api(app, token, "GET", path, doing, params={"per_page": 1, "page": 1})
    if response.status_code != 200:
        raise _refused(response, doing)
    data = app._json(response, doing)
    commits = data.get("commits") if isinstance(data.get("commits"), list) else []
    first = commits[0] if commits and isinstance(commits[0], dict) else {}
    oldest = ((first.get("commit") or {}).get("committer") or {}).get("date")
    ahead, behind = data.get("ahead_by"), data.get("behind_by")
    merge_base = (data.get("merge_base_commit") or {}).get("sha")
    return {
        "status": data.get("status") if isinstance(data.get("status"), str) else None,
        "ahead_by": ahead if type(ahead) is int else None,
        "behind_by": behind if type(behind) is int else None,
        "oldest": oldest if isinstance(oldest, str) else None,
        "merge_base": merge_base if isinstance(merge_base, str) else None,
    }


async def compare_commits(
    app: GitHubApp, token: str, owner: str, repo: str, base: str, head: str, *, page: int, pages: int
) -> tuple[list[dict], int | None]:
    """The commits ``head`` of owner/repo has that ``base`` lacks, oldest first, as GitHub's comparison lists them
    ``page`` at a time over at most ``pages`` pages (GitHub lists 250 at most), and how many there are in all
    (``total_commits``; None when GitHub does not say)."""
    doing = f"listing the commits from {base[:12]} to {head[:12]} on {owner}/{repo}"
    path = _path("repos", owner, repo, "compare", f"{base}...{head}")
    found: list[dict] = []
    total = None
    for number in range(1, pages + 1):
        response = await _api(app, token, "GET", path, doing, params={"per_page": page, "page": number})
        if response.status_code != 200:
            raise _refused(response, doing)
        data = app._json(response, doing)
        listed = data.get("commits") if isinstance(data.get("commits"), list) else []
        found += [item for item in listed if isinstance(item, dict)]
        total = data.get("total_commits") if type(data.get("total_commits")) is int else total
        if len(listed) < page or (total is not None and len(found) >= total):
            break
    return found, total


async def create_ref(app: GitHubApp, token: str, owner: str, repo: str, ref: str, sha: str) -> bool:
    """Make the ref ``ref`` (refs/tags/<tag>) of owner/repo point at commit ``sha``: True when it was made now, False
    when GitHub says it exists already; ``Refused`` for any other refusal."""
    doing = f"making {ref} of {owner}/{repo} at {sha[:12]}"
    payload = {"ref": ref, "sha": sha}
    response = await _api(app, token, "POST", _path("repos", owner, repo, "git", "refs"), doing, json=payload)
    if response.status_code == 201:
        return True
    refusal = _refused(response, doing)
    if response.status_code == 422 and "already exists" in str(refusal).lower():
        return False
    raise refusal


async def file_text(app: GitHubApp, token: str, owner: str, repo: str, path: str, ref: str) -> str | None:
    """The text of file ``path`` of owner/repo at commit ``ref``; None when the commit has no such file. A file GitHub
    does not answer whole (over 1 MB), or that is not UTF-8, is a ``Refused``: it is never read in part."""
    doing = f"reading {path} at {ref[:12]} of {owner}/{repo}"
    response = await _api(
        app, token, "GET", _path("repos", owner, repo, "contents", *path.split("/")), doing, params={"ref": ref}
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _refused(response, doing)
    data = app._json(response, doing)
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
        raise Refused(f"GitHub did not answer the whole file while {doing}")
    try:
        return base64.b64decode(content or "", validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise Refused(f"GitHub answered a file that is not UTF-8 text while {doing}") from None


async def directory_files(
    app: GitHubApp, token: str, owner: str, repo: str, sha: str, directory: str, *, most: int, most_bytes: int
) -> list[tuple[str, bool, bytes]]:
    """The files under ``directory`` of commit ``sha`` of owner/repo, each (its path within the directory, whether it
    is executable, its bytes), in path order: the commit's tree, whole, then each file's blob. A tree GitHub cuts, a
    symbolic link or a submodule there, more than ``most`` files or ``most_bytes`` bytes, or a blob GitHub does not
    hand whole is a ``Refused``: a directory is never read in part."""
    doing = f"reading {directory} at {sha[:12]} of {owner}/{repo}"
    where = _path("repos", owner, repo, "git", "trees", sha)
    response = await _api(app, token, "GET", where, doing, params={"recursive": "1"})
    if response.status_code != 200:
        raise _refused(response, doing)
    data = app._json(response, doing)
    if data.get("truncated") is True or not isinstance(data.get("tree"), list):
        raise Refused(f"GitHub did not list the whole tree while {doing}")
    prefix = directory.rstrip("/") + "/"
    entries = [item for item in data["tree"] if isinstance(item, dict) and str(item.get("path")).startswith(prefix)]
    files = [item for item in entries if item.get("type") != "tree"]
    odd = next((item for item in files if item.get("type") != "blob" or item.get("mode") not in FILE_MODES), None)
    if odd is not None:
        raise Refused(f"{odd.get('path')} is a symbolic link or a submodule, which a skill's bundle never holds")
    if len(files) > most or sum(int(item.get("size") or 0) for item in files) > most_bytes:
        raise Refused(f"{directory} holds more than {most} files or {most_bytes} bytes at {sha[:12]}")
    found = []
    for item in sorted(files, key=lambda entry: entry["path"]):
        reading = f"reading {item['path']} at {sha[:12]} of {owner}/{repo}"
        answer = await _api(app, token, "GET", _path("repos", owner, repo, "git", "blobs", item["sha"]), reading)
        if answer.status_code != 200:
            raise _refused(answer, reading)
        blob = app._json(answer, reading)
        try:
            content = base64.b64decode(blob.get("content") or "", validate=False)
        except ValueError:
            content = None
        if blob.get("encoding") != "base64" or content is None or len(content) != item.get("size", len(content)):
            raise Refused(f"GitHub did not answer the whole file while {reading}")
        found.append((item["path"][len(prefix) :], item.get("mode") == "100755", content))
    return found


async def find_pull(
    app: GitHubApp, token: str, owner: str, repo: str, branch: str, base: str | None = None
) -> dict | None:
    """The open pull request of ``branch`` (of owner/repo itself), into ``base`` when it is given, or None."""
    doing = f"looking for the pull request of {branch} on {owner}/{repo}"
    params = {"head": f"{owner}:{branch}", "state": "open", "per_page": 1}
    if base is not None:
        params["base"] = base
    response = await _api(app, token, "GET", _path("repos", owner, repo, "pulls"), doing, params=params)
    if response.status_code != 200:
        raise _refused(response, doing)
    items = response.json()
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None


async def open_pull(
    app: GitHubApp,
    token: str,
    owner: str,
    repo: str,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool = False,
) -> dict:
    """The pull request of ``branch`` into ``base``, a draft with ``draft``: opened now, or the one open already into
    that same base (one into another base is never it). Tried again after each of HEAD_WAITS while GitHub says the head
    is not there; ``Refused`` otherwise."""
    doing = f"opening a pull request of {branch} on {owner}/{repo}"
    payload = {"title": title, "head": branch, "base": base, "body": body, "maintainer_can_modify": False}
    if draft:
        payload["draft"] = True
    waits = list(HEAD_WAITS)
    while True:
        response = await _api(app, token, "POST", _path("repos", owner, repo, "pulls"), doing, json=payload)
        if response.status_code == 201:
            return app._json(response, doing)
        refusal = _refused(response, doing)
        if response.status_code != 422:
            raise refusal
        found = await find_pull(app, token, owner, repo, branch, base)
        if found is not None:
            return found
        if not (waits and head_missing(refusal.errors)):
            raise refusal
        log.info("github does not see the head yet", extra={"branch": branch, "wait_s": waits[0]})
        await asyncio.sleep(waits.pop(0))


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
