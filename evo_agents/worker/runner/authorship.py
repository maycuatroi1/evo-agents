"""Who a run's commits name: the agent as author, the person at the origin of the run as co-author.

A chat run, under any profile, and a run under a profile that is not its project's default (the claim's
``own_home``) are the agent's work: the commits of such a run name the profile as their author, its name with the email
``<handle>@<host of the hub>`` (``agent_author``). The committer stays this machine's git identity. The commit the
daemon makes of what the agent left (``push.commit``) carries the trailer ``Co-authored-by: <login>
<<github_id>+<login>@users.noreply.github.com>`` of the person who dispatched the run (the claim's ``origin``;
``co_author_trailer``), and the agent's own commits in the run take the same author from GIT_AUTHOR_NAME and
GIT_AUTHOR_EMAIL in its environment (``author_env``). Any other run under the project's default profile, a run without
a profile and a run whose claim has none of this commit as before: no author, no trailer, no variable. The daemon never
rewrites a commit that is already there.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

NOREPLY = "users.noreply.github.com"


@dataclass(frozen=True)
class Author:
    """The git identity a run's commits name as their author."""

    name: str
    email: str

    @property
    def flag(self) -> str:
        """The value of ``git commit --author``."""
        return f"{self.name} <{self.email}>"


def hub_url(run) -> str:
    """The URL of the hub the daemon of ``run`` works for; empty when it has none."""
    return str(getattr(getattr(run.daemon, "hub", None), "url", "") or "")


def agent_authored(spec: dict) -> bool:
    """Whether the run of claim ``spec`` is the agent's work: a chat run, or one under a profile that is not its
    project's default, with a profile in its claim."""
    return isinstance(spec.get("profile"), dict) and (spec.get("kind") == "chat" or bool(spec.get("own_home")))


def _host(hub_url: str) -> str:
    return (urlsplit(hub_url).hostname or "").lower()


def agent_author(spec: dict, hub_url: str) -> Author | None:
    """The author of the commits of the run of claim ``spec``, under the hub at ``hub_url``: its profile, named as it
    is with the email ``<handle>@<host of the hub>``; None for a run that is not the agent's work (``agent_authored``)
    or a hub URL without a host."""
    if not agent_authored(spec):
        return None
    profile = spec["profile"]
    handle = str(profile.get("handle") or "").strip()
    host = _host(hub_url)
    if not handle or not host:
        return None
    name = " ".join(str(profile.get("name") or "").split()) or handle
    return Author(name=name, email=f"{handle}@{host}")


def co_author_trailer(spec: dict) -> str | None:
    """The ``Co-authored-by`` trailer that names the person at the origin of the run of claim ``spec``, with their
    GitHub noreply email (``<github_id>+<login>@`` NOREPLY, or ``<login>@`` NOREPLY without an id); None for a run that
    is not the agent's work or a claim without an origin."""
    origin = spec.get("origin")
    if not agent_authored(spec) or not isinstance(origin, dict):
        return None
    login = str(origin.get("login") or "").strip()
    if not login:
        return None
    github_id = origin.get("github_id")
    local = f"{github_id}+{login}" if isinstance(github_id, int) and not isinstance(github_id, bool) else login
    return f"Co-authored-by: {login} <{local}@{NOREPLY}>"


def commit_message(spec: dict, subject: str) -> str:
    """The message of the daemon's commit of the run of claim ``spec``: ``subject``, and the co-author trailer after a
    blank line when the run has one."""
    trailer = co_author_trailer(spec)
    return f"{subject}\n\n{trailer}" if trailer else subject


def author_env(spec: dict, hub_url: str) -> dict[str, str]:
    """GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL of the agent's author for the environment of its agent; empty for a run
    that is not the agent's work, whose environment stays as it was."""
    author = agent_author(spec, hub_url)
    if author is None:
        return {}
    return {"GIT_AUTHOR_NAME": author.name, "GIT_AUTHOR_EMAIL": author.email}
