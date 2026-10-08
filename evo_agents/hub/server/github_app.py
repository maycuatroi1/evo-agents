"""The hub's GitHub App: installation tokens for the repos of a run (``docs/credentials.md``).

Each call to the App's routes carries a JWT the hub signs with the App's private key (EVO_HUB_GITHUB_APP_PRIVATE_KEY,
RS256): issued 60 seconds back, for a hub clock ahead of GitHub's, and ending 9 minutes from now, under the 10 minutes
GitHub allows. With it the hub finds the App's installation that covers a repo (GET /repos/{owner}/{repo}/installation),
kept 10 minutes per owner/repo, and asks that installation for a token (POST /app/installations/{id}/access_tokens)
naming the run's repos of that installation and the permissions ``GITHUB_PERMISSIONS``, nothing more (a review run's
``GITHUB_READ_PERMISSIONS``, which only read): GitHub makes a token that lives an hour and opens those repos alone.
A token is revoked with itself (DELETE /installation/token).

The calls go through ``GitHubClient`` of ``evo_agents.hub.server.github``: a timeout each, no redirect followed, one log
line with the method, the path and the status, GitHub failing or not answering as ``GitHubUnavailable``. GitHub
refusing a token of an installation is ``GitHubRefused``; GitHub refusing the App's JWT means the hub's configuration is
wrong, and is ``GitHubUnavailable``, as a refused client secret is. Neither a JWT nor a token goes into a URL, a log
line or an error, and ``InstallationToken`` leaves its token out of its repr.

``GitHubApp.tokens`` is what the lease route asks: a token per installation, and for each repo left without one the
reason, such as "the GitHub App is not installed on owner/repo". A repo the App is not installed on is not cached, so
installing the App counts at the next ask.

The App is installed on accounts, not given to members: any member who registers a repo of an account the App is
installed on would get a token for it. So ``tokens`` asks for the run's owner too, the hub user whose login is their
GitHub login, and hands out a token only for the repos that account may push to: GitHub's answer to GET
/repos/{owner}/{repo}/collaborators/{login}/permission, asked with the token just made (which opens that repo, with
``metadata: read``), must be ``write``, ``maintain`` or ``admin``, for the account of the user's GitHub id. A repo it
may not push to is left without a token, with the reason "<login> cannot push to owner/repo on GitHub: ...", and the
token made with it is revoked and made again for the other repos of its installation. An answer that allows is kept
``PERMISSION_CACHE_SECONDS`` per login and owner/repo; a refusal is not kept, so access granted on GitHub counts at the
next ask.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from evo_agents.hub.config import GITHUB_APP_VARIABLES, LOGIN, ConfigError, HubConfig
from evo_agents.hub.credentials import GITHUB_PERMISSIONS
from evo_agents.hub.server.github import API_HEADERS, GitHubClient, GitHubRefused, GitHubUnavailable

JWT_BACKDATE_SECONDS = 60  # iat this far back, for a hub clock ahead of GitHub's
JWT_LIFETIME_SECONDS = 9 * 60  # exp this far ahead; GitHub refuses a JWT ending more than 10 minutes from its now
JWT_HEADER = {"alg": "RS256", "typ": "JWT"}
INSTALLATION_CACHE_SECONDS = 600  # how long the installation found for an owner/repo is kept
PERMISSION_CACHE_SECONDS = 300  # how long GitHub saying a login may push to an owner/repo is kept; a refusal is not
PUSH_PERMISSIONS = frozenset({"write", "maintain", "admin"})  # a role on a repo that may push to it
PERMISSIONS_TEXT = ", ".join(f"{name}: {level}" for name, level in GITHUB_PERMISSIONS.items())

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Installation:
    """The App installed on one GitHub account, for some or all of its repos."""

    id: int
    account: str  # the login it is installed on


@dataclass(frozen=True)
class InstallationToken:
    """A token of one installation for some of its repos; ``repr`` leaves the token out."""

    installation: Installation
    repositories: tuple[str, ...]  # the repos it opens, by name, all of ``installation.account``
    permissions: dict[str, str]  # what GitHub granted, GITHUB_PERMISSIONS
    expires_at: datetime
    token: str = field(repr=False)

    @property
    def name(self) -> str:
        """The name of a lease of this token, as ``evo_agents.hub.credentials.Lease`` gives it."""
        return f"github-app:{self.installation.account}"


@dataclass(frozen=True)
class AppTokens:
    """What ``GitHubApp.tokens`` answers: the tokens made, and why each repo left without one has none."""

    tokens: list[InstallationToken]
    missing: dict[str, str]  # "owner/repo" -> reason


@dataclass(frozen=True)
class Pusher:
    """The GitHub account a token is made for: the hub user who dispatched the run, by login and GitHub id."""

    login: str
    github_id: int | None = None  # None for a user who never signed in: the login alone is checked


def not_installed(owner: str, repo: str) -> str:
    return f"the GitHub App is not installed on {owner}/{repo}"


def cannot_push(login: str, owner: str, repo: str, why: str) -> str:
    return f"{login} cannot push to {owner}/{repo} on GitHub: {why}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64_json(value: dict) -> str:
    return _b64(json.dumps(value, separators=(",", ":")).encode())


def _timestamp(value) -> datetime | None:
    """A time GitHub wrote in ISO 8601 (``2026-10-07T10:00:00Z``), or None when it is not one with a time zone."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _private_key(pem: str) -> rsa.RSAPrivateKey:
    """The App's RSA key from its PEM; ``ConfigError`` naming the variable, never the key, when it does not open."""
    name = GITHUB_APP_VARIABLES[1]
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):  # TypeError: a key that wants a password
        raise ConfigError(name, f"{name} does not open as a private key without a password") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConfigError(name, f"{name} must be an RSA key, as GitHub gives an App: its JWT is signed with RS256")
    return key


class GitHubApp(GitHubClient):
    """The hub's GitHub App, signing as EVO_HUB_GITHUB_APP_ID with EVO_HUB_GITHUB_APP_PRIVATE_KEY.

    ``clock`` (monotonic seconds) times the installation cache and ``now`` (epoch seconds) dates the JWT; tests move
    them."""

    def __init__(
        self,
        config: HubConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ):
        missing = config.github_app_missing()
        if missing:
            raise ValueError(f"the GitHub App is not configured: {', '.join(missing)} not set")
        self._key = _private_key(config.github_app_private_key)  # before the HTTP client, which would need closing
        app_id = config.github_app_id
        self._issuer: int | str = int(app_id) if app_id.isdigit() else app_id  # an App ID as a number, a client ID
        self._clock, self._now = clock, now
        self._installations: dict[tuple[str, str], tuple[float, Installation]] = {}
        self._pushers: dict[tuple[str, int | None, str, str], float] = {}  # (login, id, owner, repo) -> kept until
        super().__init__(config, transport)

    @classmethod
    def from_config(cls, config: HubConfig, transport: httpx.AsyncBaseTransport | None = None) -> GitHubApp | None:
        """The hub's App, or None when EVO_HUB_GITHUB_APP_ID and EVO_HUB_GITHUB_APP_PRIVATE_KEY are not set."""
        return None if config.github_app_missing() else cls(config, transport)

    def __repr__(self) -> str:
        return f"GitHubApp(issuer={self._issuer!r})"

    def app_jwt(self) -> str:
        """A JWT of the App for GitHub's /app routes: RS256, from 60 seconds ago to 9 minutes from now."""
        now = int(self._now())
        claims = {"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_LIFETIME_SECONDS, "iss": self._issuer}
        signing_input = f"{_b64_json(JWT_HEADER)}.{_b64_json(claims)}"
        signature = self._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64(signature)}"

    def _app_headers(self) -> dict:
        return {**API_HEADERS, "Authorization": f"Bearer {self.app_jwt()}"}

    @staticmethod
    def _refused_app(response: httpx.Response, doing: str) -> None:
        if response.status_code == 401:
            id_name, key_name = GITHUB_APP_VARIABLES
            raise GitHubUnavailable(
                f"GitHub refused the hub's GitHub App while {doing}: {id_name} and {key_name} must be the App's ID "
                "and one of its private keys; an admin must fix the hub's configuration"
            )

    def _forget(self, installation_id: int) -> None:
        for key, (_, found) in list(self._installations.items()):
            if found.id == installation_id:
                del self._installations[key]

    async def installation(self, owner: str, repo: str) -> Installation | None:
        """The App's installation that covers owner/repo, or None when the App is not installed on it.

        An installation found is kept ``INSTALLATION_CACHE_SECONDS`` for this owner/repo; a 404 is not kept."""
        key = (owner.lower(), repo.lower())
        cached = self._installations.get(key)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        doing = f"finding the GitHub App's installation on {owner}/{repo}"
        url = f"{self.config.github_api_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation"
        response = await self._call("GET", url, doing, headers=self._app_headers())
        if response.status_code == 404:
            self._installations.pop(key, None)
            return None
        self._refused_app(response, doing)
        if response.status_code != 200:
            raise GitHubUnavailable(f"GitHub answered {response.status_code} while {doing}")
        data = self._json(response, doing)
        installation_id, account = data.get("id"), (data.get("account") or {}).get("login")
        if type(installation_id) is not int or installation_id <= 0:
            raise GitHubUnavailable(f"GitHub answered without a usable installation id while {doing}")
        login = account if isinstance(account, str) and LOGIN.fullmatch(account) else owner
        found = Installation(installation_id, login)
        self._installations[key] = (self._clock() + INSTALLATION_CACHE_SECONDS, found)
        return found

    async def create_token(
        self, installation: Installation, repos: Iterable[str], permissions: dict[str, str] | None = None
    ) -> InstallationToken:
        """A token of ``installation`` for ``repos`` (names of its account's repos) alone, with ``permissions``
        (GITHUB_PERMISSIONS by default; ``GITHUB_READ_PERMISSIONS`` for a review run).

        GitHub refusing it raises ``GitHubRefused``, and the installations kept for its repos are dropped, so the next
        ask finds them again."""
        names = tuple(sorted(dict.fromkeys(repos)))
        if not names:
            raise ValueError("a token is asked for at least one repo")
        permissions = dict(permissions or GITHUB_PERMISSIONS)
        account = installation.account
        full = ", ".join(f"{account}/{name}" for name in names)
        doing = f"asking the GitHub App for a token for {full}"
        response = await self._call(
            "POST",
            f"{self.config.github_api_url}/app/installations/{installation.id}/access_tokens",
            doing,
            headers=self._app_headers(),
            json={"repositories": list(names), "permissions": permissions},
        )
        status = response.status_code
        self._refused_app(response, doing)
        if status in (403, 404, 422):
            self._forget(installation.id)
        if status == 404:
            raise GitHubRefused(f"the GitHub App is no longer installed on {account}")
        if status == 403:
            raise GitHubRefused(f"GitHub refused a token for {full}: the App's installation on {account} is suspended")
        if status == 422:
            raise GitHubRefused(
                f"GitHub refused a token for {full}: the App's installation on {account} does not cover each of them "
                f"with {PERMISSIONS_TEXT}"
            )
        if status != 201:
            raise GitHubUnavailable(f"GitHub answered {status} while {doing}")
        data = self._json(response, doing)
        token, expires_at = data.get("token"), _timestamp(data.get("expires_at"))
        if not (isinstance(token, str) and token) or expires_at is None:
            raise GitHubUnavailable(f"GitHub answered without a usable token and expires_at while {doing}")
        granted = data.get("permissions")
        return InstallationToken(
            installation, names, dict(granted) if isinstance(granted, dict) else permissions, expires_at, token
        )

    async def revoke(self, token: str) -> bool:
        """Revoke an installation token with itself (DELETE /installation/token). True when GitHub revoked it, False
        when GitHub no longer takes it: it expired or was revoked before."""
        doing = "revoking a token of the GitHub App"
        headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
        url = f"{self.config.github_api_url}/installation/token"
        response = await self._call("DELETE", url, doing, headers=headers)
        if response.status_code == 204:
            return True
        if response.status_code == 401:
            return False
        raise GitHubUnavailable(f"GitHub answered {response.status_code} while {doing}")

    async def push_refusal(self, token: str, owner: str, repo: str, pusher: Pusher) -> str | None:
        """Why ``pusher`` may not push to owner/repo, or None when GitHub says it may: its permission there (GET
        /repos/{owner}/{repo}/collaborators/{login}/permission, asked with installation ``token``, which must open the
        repo) is write, maintain or admin, and GitHub's account of that login has the pusher's GitHub id.

        An answer that allows is kept PERMISSION_CACHE_SECONDS; GitHub failing raises ``GitHubUnavailable``."""
        key = (pusher.login.lower(), pusher.github_id, owner.lower(), repo.lower())
        until = self._pushers.get(key)
        if until is not None and until > self._clock():
            return None
        doing = f"asking GitHub whether {pusher.login} may push to {owner}/{repo}"
        path = "/".join(quote(part, safe="") for part in (owner, repo, "collaborators", pusher.login, "permission"))
        headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
        response = await self._call("GET", f"{self.config.github_api_url}/repos/{path}", doing, headers=headers)
        if response.status_code == 404:
            return cannot_push(pusher.login, owner, repo, "GitHub knows no collaborator of that login there")
        if response.status_code != 200:
            raise GitHubUnavailable(f"GitHub answered {response.status_code} while {doing}")
        data = self._json(response, doing)
        role, permission = data.get("role_name"), data.get("permission")
        account = data.get("user") if isinstance(data.get("user"), dict) else {}
        if pusher.github_id is not None and account.get("id") != pusher.github_id:
            return cannot_push(
                pusher.login,
                owner,
                repo,
                f"the GitHub account named {pusher.login} now is not the one that signed in to the hub; sign in again",
            )
        if role not in PUSH_PERMISSIONS and permission not in PUSH_PERMISSIONS:
            held = role or permission or "none"
            return cannot_push(pusher.login, owner, repo, f"their role there is {held}, and pushing needs write")
        self._pushers[key] = self._clock() + PERMISSION_CACHE_SECONDS
        return None

    async def _token_for_pushers(
        self,
        installation: Installation,
        members: list[tuple[str, str]],
        pusher: Pusher,
        missing: dict[str, str],
        permissions: dict[str, str] | None = None,
    ) -> InstallationToken | None:
        """A token of ``installation`` for those of ``members`` (owner, repo) ``pusher`` may push to, or None when it
        may push to none; the reason of each other one goes into ``missing``. The token made for all of them answers
        the question for the repos not kept yet, and is revoked and made again without the ones refused.
        ``GitHubRefused`` and ``GitHubUnavailable`` go to the caller, with any token made here revoked."""
        token = await self.create_token(installation, [repo for _, repo in members], permissions)
        allowed = []
        try:
            for owner, repo in members:
                refusal = await self.push_refusal(token.token, owner, repo, pusher)
                if refusal is None:
                    allowed.append((owner, repo))
                else:
                    missing[f"{owner}/{repo}"] = refusal
        except GitHubUnavailable:
            await self._revoke_quietly(token)
            raise
        if len(allowed) == len(members):
            return token
        await self._revoke_quietly(token)
        if not allowed:
            return None
        return await self.create_token(installation, [repo for _, repo in allowed], permissions)

    async def _revoke_quietly(self, token: InstallationToken) -> None:
        """Revoke a token made for a repo its pusher may not push to; GitHub failing leaves it to expire unused."""
        try:
            await self.revoke(token.token)
        except GitHubUnavailable as exc:
            log.warning("a GitHub token made and not handed out is left to expire", extra={"why": str(exc)})

    async def tokens(
        self, repos: Iterable[tuple[str, str]], pusher: Pusher, permissions: dict[str, str] | None = None
    ) -> AppTokens:
        """A token for each installation that covers some of ``repos`` ((owner, repo) on github.com), naming those
        repos alone, and only those ``pusher`` may push to (``push_refusal``), with ``permissions`` (GITHUB_PERMISSIONS
        by default); the reason each repo left without one has none.

        It never raises for GitHub: a refusal is the reason of the repos it concerns, and GitHub failing or not
        answering is the reason of that repo and of every one not asked yet, which are not asked."""
        wanted: dict[tuple[str, str], tuple[str, str]] = {}
        for owner, repo in repos:
            wanted.setdefault((owner.lower(), repo.lower()), (owner, repo))
        missing: dict[str, str] = {}
        groups: dict[int, tuple[Installation, list[tuple[str, str]]]] = {}
        outage: str | None = None
        for owner, repo in wanted.values():
            if outage is not None:
                missing[f"{owner}/{repo}"] = outage
                continue
            try:
                found = await self.installation(owner, repo)
            except GitHubUnavailable as exc:
                outage = missing[f"{owner}/{repo}"] = str(exc)
                continue
            if found is None:
                missing[f"{owner}/{repo}"] = not_installed(owner, repo)
            else:
                groups.setdefault(found.id, (found, []))[1].append((owner, repo))
        made: list[InstallationToken] = []
        for installation, members in groups.values():
            reason = outage
            if reason is None:
                try:
                    token = await self._token_for_pushers(installation, members, pusher, missing, permissions)
                    if token is not None:
                        made.append(token)
                    continue
                except GitHubUnavailable as exc:
                    outage = reason = str(exc)
                except GitHubRefused as exc:
                    reason = str(exc)
            for owner, repo in members:
                missing.setdefault(f"{owner}/{repo}", reason)  # a refusal found before GitHub failed stays
        return AppTokens(made, missing)
