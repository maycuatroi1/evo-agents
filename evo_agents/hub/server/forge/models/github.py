"""What the hub's calls to GitHub share: their errors, the account a token is of, the headers of GitHub's REST API,
and a repo's owner/name there (``Target``). The clients of ``evo_agents.hub.server.forge.client`` raise and give these.

GitHub not answering, answering 5xx or rate limiting is ``GitHubUnavailable``, which the API turns into a 502 naming
GitHub; GitHub turning down a token or a code is ``GitHubRefused``. Their messages are safe to show.
"""

from __future__ import annotations

from dataclasses import dataclass

API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# Code exchange errors that mean the hub's own configuration is wrong, not the person's sign-in.
CONFIG_ERRORS = {
    "incorrect_client_credentials": "GitHub refused the hub's client id or secret",
    "redirect_uri_mismatch": "the app on GitHub does not list EVO_HUB_PUBLIC_URL/v1/auth/web/callback as a callback",
}


class GitHubRefused(Exception):
    """GitHub turned down a token or a code; the message is safe to show."""


class GitHubUnavailable(Exception):
    """GitHub did not answer in time, failed, or answered in a way the hub cannot use; safe to show."""


@dataclass(frozen=True)
class GitHubUser:
    login: str
    id: int


@dataclass(frozen=True)
class Target:
    """A repo on GitHub, by owner and name."""

    owner: str
    name: str
