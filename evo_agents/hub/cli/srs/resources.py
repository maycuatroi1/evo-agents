"""The routes of the core plugin srs ``evo-agents hub srs`` asks, under /v1/x/srs/{project}/ (``evo_agents.hub.server.srs``,
which this client never imports): :class:`Documents`, the one gateway, and :class:`Web`, the links of the hub's web.

A refusal lists on stderr, one line each, every problem of a check, conflict or finding the hub found. Standard
library only, like the rest of the client.
"""

from __future__ import annotations

from typing import Any

from evo_agents.hub.cli.base import HubContext
from evo_agents.hub.cli.features import PluginRoutes


def problem_line(problem: Any) -> str | None:
    """The line of one problem a refusal lists; None for what is none of a check, a conflict or a finding."""
    if isinstance(problem, dict) and "check" in problem:
        where = f" line {problem['line']}" if problem.get("line") else ""
        code = f" {problem['code']}" if problem.get("code") else ""
        return f"  {problem['level']}: {problem['check']}{code}{where}: {problem['message']}"
    if isinstance(problem, dict) and "latest" in problem:
        return f"  conflict on {problem.get('code') or problem.get('item') or 'the preamble'}"
    if isinstance(problem, dict) and "finding" in problem:
        chat = f" ({problem['chat_link']})" if problem.get("chat_link") else ""
        return f"  finding {problem['finding']}, {problem['kind']}: {problem['summary']}{chat}"
    return None


class Documents(PluginRoutes):
    """The BA documents of a project, their versions, reviews, comments and findings (Gateway)."""

    feature = "srs"

    def problems(self, detail: Any) -> list[str]:
        return [line for line in map(problem_line, detail) if line is not None]

    # Documents and items

    def list(self, everything: bool) -> list[dict]:
        return self.call("GET", "documents", query={"all": "true" if everything else None})

    def get(self, slug: str, version: int | None = None) -> dict:
        return self.call("GET", "documents", slug, query={"version": version})

    def latest(self, slug: str) -> dict:
        """The document ``slug``, read for its latest version; HubError when the hub answered no object."""
        return self.object("GET", "documents", slug)

    def item(self, slug: str, ref: str, version: int | None) -> dict:
        return self.call("GET", "documents", slug, "items", ref, query={"version": version})

    def import_(self, body: dict) -> dict:
        return self.call("POST", "import", body=body)

    def export(self, slug: str, version: int | None, anchors: bool) -> dict:
        query = {"version": version, "anchors": None if anchors else "false"}
        return self.call("GET", "documents", slug, "export", query=query)

    def retire(self, slug: str, body: dict) -> dict:
        return self.call("POST", "documents", slug, "retire", body=body)

    def archive(self, slug: str, verb: str) -> dict:
        """POST .../documents/{slug}/archive or /unarchive, as ``verb`` says."""
        return self.call("POST", "documents", slug, verb)

    # Versions and reviews

    def version(self, slug: str, number: int) -> dict:
        return self.call("GET", "documents", slug, "versions", str(number))

    def submit(self, slug: str, number: int) -> dict:
        return self.object("POST", "documents", slug, "versions", str(number), "submit")

    def submit_use_case(self, slug: str, ref: str) -> dict:
        return self.object("POST", "documents", slug, "use-cases", ref, "submit")

    def review(self, slug: str, number: int, body: dict) -> dict:
        return self.call("POST", "documents", slug, "versions", str(number), "reviews", body=body)

    def review_item(self, slug: str, number: int, ref: str, body: dict) -> dict:
        return self.call("POST", "documents", slug, "versions", str(number), "items", ref, "reviews", body=body)

    def withdraw(self, slug: str, number: int, body: dict) -> dict:
        return self.call("POST", "documents", slug, "versions", str(number), "withdraw", body=body)

    def queue(self) -> list[dict]:
        return self.call("GET", "reviews")

    # Comments and findings

    def comments(self, slug: str, query: dict) -> list[dict]:
        return self.call("GET", "documents", slug, "comments", query=query)

    def comment(self, slug: str, body: dict) -> dict:
        return self.object("POST", "documents", slug, "comments", body=body)

    def settle_thread(self, slug: str, comment: int, verb: str) -> dict:
        """POST .../comments/{id}/resolve or /reopen, as ``verb`` says."""
        return self.object("POST", "documents", slug, "comments", str(comment), verb)

    def findings(self, slug: str, status: str | None) -> list[dict]:
        return self.call("GET", "documents", slug, "findings", query={"status": status})

    def waive(self, slug: str, finding: int, reason: str) -> dict:
        return self.call("POST", "documents", slug, "findings", str(finding), "waive", body={"reason": reason})


class Web:
    """The pages of the hub's web this client is signed in to: a path (``/p/...``) as a whole URL."""

    def __init__(self, ctx: HubContext):
        self.ctx = ctx

    def __call__(self, link: str) -> str:
        if not link:
            return ""
        url = self.ctx.credentials.url  # read the first time a link is shown, as the hub's address
        return f"{url.rstrip('/')}{link}" if link.startswith("/") else link
