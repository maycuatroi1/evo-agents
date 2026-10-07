"""The hub's calls to GitHub: whose a token is, whether it was issued to this OAuth App, and the web flow's code
exchange. The base URLs come from the configuration, so tests and Playwright can point the hub at a fake GitHub.

Every call has a timeout (EVO_HUB_GITHUB_TIMEOUT) and never follows a redirect. GitHub not answering, answering
5xx or rate limiting raises ``GitHubUnavailable``, which the API turns into a 502 naming GitHub; GitHub turning
down a token or a code raises ``GitHubRefused``. Tokens, codes and the client secret travel only in headers and
form bodies, never in a URL, and the one log line per call carries the method, the path and the status.
``GitHubClient`` holds those rules; ``GitHub`` here and the GitHub App of ``evo_agents.hub.server.github_app``
build on it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from evo_agents import __version__
from evo_agents.hub.config import LOGIN, HubConfig

log = logging.getLogger(__name__)

API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")  # an OAuth error code, safe to repeat in a message
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


def _error_code(value) -> str:
    return value if isinstance(value, str) and _ERROR_CODE.fullmatch(value) else "unknown_error"


class GitHubClient:
    """HTTP to GitHub as the hub calls it: a timeout per call, no redirect followed, one log line per call, and
    GitHub failing, rate limiting or not answering as ``GitHubUnavailable``."""

    def __init__(self, config: HubConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(config.github_timeout),
            follow_redirects=False,
            headers={"User-Agent": f"evo-agents-hub/{__version__}", "Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, url: str, doing: str, **kwargs) -> httpx.Response:
        path = urlsplit(url).path
        started = time.perf_counter()
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException:
            log.warning("github call timed out", extra={"method": method, "path": path})
            raise GitHubUnavailable(
                f"GitHub did not answer within {self.config.github_timeout:g}s while {doing}; try again shortly"
            ) from None
        except httpx.HTTPError as exc:
            log.warning("github call failed", extra={"method": method, "path": path, "error": type(exc).__name__})
            reason = type(exc).__name__
            raise GitHubUnavailable(f"cannot reach GitHub while {doing} ({reason}); try again shortly") from None
        status = response.status_code
        duration = round((time.perf_counter() - started) * 1000, 1)
        log.info("github call", extra={"method": method, "path": path, "status": status, "duration_ms": duration})
        limited = status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0")
        if status >= 500 or limited:
            raise GitHubUnavailable(f"GitHub answered {status} while {doing}; try again shortly")
        if 300 <= status < 400:
            raise GitHubUnavailable(f"GitHub answered {status} with a redirect while {doing}: check the GitHub URLs")
        return response

    @staticmethod
    def _json(response: httpx.Response, doing: str) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise GitHubUnavailable(f"GitHub answered {response.status_code} without a JSON object while {doing}")
        return data


class GitHub(GitHubClient):
    """The hub's OAuth App: whose a token is, whether this app issued it, and the web flow's code exchange."""

    async def user(self, token: str) -> GitHubUser:
        """The account behind ``token`` (GET /user)."""
        doing = "reading the signed-in user"
        headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
        response = await self._call("GET", f"{self.config.github_api_url}/user", doing, headers=headers)
        if response.status_code in (401, 403):
            raise GitHubRefused("GitHub did not accept the token")
        if response.status_code != 200:
            raise GitHubUnavailable(f"GitHub answered {response.status_code} while {doing}")
        data = self._json(response, doing)
        login, user_id = data.get("login"), data.get("id")
        if not (isinstance(login, str) and LOGIN.fullmatch(login)) or type(user_id) is not int or user_id <= 0:
            raise GitHubUnavailable("GitHub answered GET /user without a usable login and id")
        return GitHubUser(login, user_id)

    async def issued_to_app(self, token: str) -> bool:
        """Whether ``token`` was issued to this hub's OAuth App (POST /applications/{client_id}/token, which needs
        the client secret). False for a token of another app, a personal token, or one GitHub does not know."""
        client_id, secret = self.config.github_client_id, self.config.github_client_secret
        doing = "checking which app the token belongs to"
        response = await self._call(
            "POST",
            f"{self.config.github_api_url}/applications/{quote(client_id, safe='')}/token",
            doing,
            auth=(client_id, secret),
            json={"access_token": token},
            headers=API_HEADERS,
        )
        if response.status_code == 200:
            return True
        if response.status_code in (404, 422):
            return False
        if response.status_code == 401:
            raise GitHubUnavailable("GitHub refused the hub's client id or secret while checking a token")
        raise GitHubUnavailable(f"GitHub answered {response.status_code} while {doing}")

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> str:
        """The user token for a web flow ``code``, proven by the PKCE ``verifier`` and the client secret."""
        doing = "exchanging the sign-in code"
        response = await self._call(
            "POST",
            f"{self.config.github_url}/login/oauth/access_token",
            doing,
            data={
                "client_id": self.config.github_client_id,
                "client_secret": self.config.github_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
        if response.status_code != 200:
            raise GitHubUnavailable(f"GitHub answered {response.status_code} while {doing}")
        data = self._json(response, doing)
        token = data.get("access_token")
        if isinstance(token, str) and token:
            return token
        error = _error_code(data.get("error"))
        if error in CONFIG_ERRORS:
            log.error("web sign-in misconfigured", extra={"github_error": error})
            raise GitHubUnavailable(f"{CONFIG_ERRORS[error]} ({error}); an admin must fix the hub's configuration")
        raise GitHubRefused(f"GitHub refused the sign-in code ({error})")
