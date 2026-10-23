"""What the plugin reads of GitHub, and how it reads a contact out of it.

The hub has no webhook from GitHub, so the job polls (``intake``): the issues and pull requests of the project's repo
(``repo`` in the config, ``owner/name``) through the REST list ``/repos/{repo}/issues``, which answers both, and its
discussions through GraphQL when the hub process has ``GITHUB_TOKEN`` (GraphQL takes no anonymous call). A
``Fetcher`` is what reads; the job takes one as an argument, so a test gives it a fake and nothing here calls out
in a test.

Who is a contact. The author of an issue, a pull request or a discussion, when GitHub says they are no member of the
repo (``author_association`` is not OWNER, MEMBER or COLLABORATOR) and no bot. A person who only replies is not a
contact: comments are never read. An issue whose title starts with ``Invite:`` is an invite request (the issue form
``.github/ISSUE_TEMPLATE/invite.yml``), by the person who opened it. Nothing of the text is kept: the title and the
body stay on GitHub, the plugin keeps the number, the link and the time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from evo_agents.core_plugins.crm import rules

MEMBER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
INVITE_PREFIX = "invite:"
PAGE = 100
MAX_PAGES = 10
API = "https://api.github.com"
TIMEOUT = 20.0
REPO = r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$"
DISCUSSIONS = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){discussions(first:100,"
    "orderBy:{field:UPDATED_AT,direction:ASC}){nodes{number url createdAt updatedAt author{login __typename} "
    "authorAssociation}}}}"
)


@dataclass(frozen=True)
class Touch:
    """One thing a person did on the repo that makes them a contact."""

    login: str
    kind: str  # invite_request, issue, pull_request or discussion
    ref: str
    url: str
    happened_at: datetime
    updated_at: datetime


GITHUB_KINDS = ("invite_request", "issue", "pull_request", "discussion")


class Fetcher(Protocol):
    async def issues(self, repo: str, since: datetime | None) -> list[dict]:
        """The issues and pull requests of ``repo`` updated at or after ``since``, oldest update first."""

    async def discussions(self, repo: str, since: datetime | None) -> list[dict]:
        """The discussions of ``repo`` updated at or after ``since``, in GraphQL's shape; none without a token."""


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _author(item: dict, member: str | None, key: str) -> str | None:
    author = item.get(key) or {}
    if not isinstance(author, dict) or author.get("type", author.get("__typename")) == "Bot":
        return None
    if (member or "").upper() in MEMBER_ASSOCIATIONS:
        return None
    return rules.login_of(str(author.get("login") or ""))


def parse_issues(items: list[dict]) -> list[Touch]:
    """The contacts' touches in a page of issues and pull requests: those of non-members, not bots."""
    found = []
    for item in items:
        login = _author(item, item.get("author_association"), "user")
        if login is None or "number" not in item:
            continue
        pull = "pull_request" in item
        invite = not pull and str(item.get("title") or "").strip().lower().startswith(INVITE_PREFIX)
        kind = "pull_request" if pull else "invite_request" if invite else "issue"
        number = item["number"]
        found.append(
            Touch(
                login,
                kind,
                f"{'pr' if pull else 'issue'}#{number}",
                str(item.get("html_url") or ""),
                _time(item["created_at"]),
                _time(item.get("updated_at") or item["created_at"]),
            )
        )
    return found


def parse_discussions(nodes: list[dict]) -> list[Touch]:
    found = []
    for node in nodes:
        login = _author(node, node.get("authorAssociation"), "author")
        if login is None or "number" not in node:
            continue
        created = _time(node["createdAt"])
        found.append(
            Touch(
                login,
                "discussion",
                f"discussion#{node['number']}",
                str(node.get("url") or ""),
                created,
                _time(node.get("updatedAt") or node["createdAt"]),
            )
        )
    return found


class HttpFetcher:
    """Reads GitHub over HTTPS, anonymously, or with ``GITHUB_TOKEN`` when the hub's environment has one."""

    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")

    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "evo-agents-crm"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def issues(self, repo: str, since: datetime | None) -> list[dict]:
        import httpx

        found: list[dict] = []
        params = {"state": "all", "sort": "updated", "direction": "asc", "per_page": PAGE}
        if since is not None:
            params["since"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with httpx.AsyncClient(base_url=API, headers=self._headers(), timeout=TIMEOUT) as client:
            for page in range(1, MAX_PAGES + 1):
                response = await client.get(f"/repos/{repo}/issues", params={**params, "page": page})
                response.raise_for_status()
                batch = response.json()
                found += [item for item in batch if isinstance(item, dict)]
                if len(batch) < PAGE:
                    break
        return found

    async def discussions(self, repo: str, since: datetime | None) -> list[dict]:
        if not self.token:
            return []
        import httpx

        owner, _, name = repo.partition("/")
        body = {"query": DISCUSSIONS, "variables": {"owner": owner, "name": name}}
        async with httpx.AsyncClient(base_url=API, headers=self._headers(), timeout=TIMEOUT) as client:
            response = await client.post("/graphql", json=body)
            response.raise_for_status()
        data = ((response.json().get("data") or {}).get("repository") or {}).get("discussions") or {}
        nodes = [node for node in data.get("nodes") or [] if isinstance(node, dict)]
        return [node for node in nodes if since is None or _time(node["updatedAt"]) >= since]

    async def account_created(self, login: str) -> datetime | None:
        """When the GitHub account ``login`` was made (``GET /users/{login}``), None when GitHub has no such user."""
        import httpx

        async with httpx.AsyncClient(base_url=API, headers=self._headers(), timeout=TIMEOUT) as client:
            response = await client.get(f"/users/{login}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        created = response.json().get("created_at")
        return _time(created) if isinstance(created, str) else None


def default_fetcher() -> Fetcher:
    return HttpFetcher()
