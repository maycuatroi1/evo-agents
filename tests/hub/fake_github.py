"""A fake GitHub for the hub tests: github.com and api.github.com in one local HTTP server, so the real GitHub is
never called.

It answers what the hub and its CLI use: the device flow (POST /login/device/code, then polling POST
/login/oauth/access_token), the web flow (GET /login/oauth/authorize redirecting back with a code, and the code
exchange checked against the client secret, the redirect URI and the PKCE S256 challenge), GET /user and POST
/applications/{client_id}/token. A test scripts what the next device flow answers, chooses who is signed in to
the fake github.com in the browser, makes every answer a 503 or slow, and reads back every request and every code
and token the fake handed out.

It also plays the hub's GitHub App: GET /repos/{owner}/{repo}/installation, POST
/app/installations/{id}/access_tokens and DELETE /installation/token. The App's JWT is checked as GitHub checks it,
against ``app_public_key`` (the public half of the key a test hands the hub), ``app_id``, and an ``exp`` in the
future but no more than 10 minutes ahead. A test installs the App with ``install``; a token is made for the repos the
request names, or for every repo of the installation when it names none, as GitHub does, with permissions no
wider than the installation's, and ``covers`` says what it opens. GET
/repos/{owner}/{repo}/collaborators/{login}/permission answers, for an installation token that opens the repo, the
role ``collaborate`` gave the login there, admin for the account the repo belongs to, and none for any other account
the fake knows (one it issued a token to, or one ``collaborate`` named); a login it does not know is 404, as GitHub
answers for a login with no account.

It plays a second App too, the Curator's (``curator_app_id``, ``curator_app_public_key``): a JWT is the App its ``iss``
names, and each App sees only its own installations (``install(..., app="curator")``). For the Curator's changes it
keeps repos (``add_repo``: default branch, branch heads, rulesets) and answers, for an installation token that opens the
repo and holds the permission each needs, as GitHub does: GET /repos/{o}/{r}; GET .../rules/branches/{branch} and
GET .../rulesets/{id}, whose ``current_user_can_bypass`` says whether the token's App is among the ruleset's bypass
Apps; POST and GET .../pulls (a pull request of a branch whose head ``push`` set, its files ``files`` set) and GET
.../pulls/{n} and .../pulls/{n}/files; GET .../commits/{sha}/check-runs and .../status (``ci`` sets them); POST
.../check-runs; and PUT .../pulls/{n}/merge, which refuses a head that moved (409) and a base branch whose ruleset
the token's App may not bypass (405), and otherwise merges.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
APP_PERMISSIONS = {"contents": "write", "metadata": "read"}  # what the fake's App holds on an installation by default
APP_TOKEN_SECONDS = 3600  # an installation token lives an hour
JWT_MAX_SECONDS = 600  # GitHub refuses an App JWT whose exp is further ahead
JWT_LEEWAY_SECONDS = 60  # an iat this far in the future still passes, for clocks apart
_LEVELS = {"read": 1, "write": 2, "admin": 3}
LEGACY_PERMISSIONS = {"admin": "admin", "maintain": "write", "write": "write", "triage": "read", "read": "read"}


@dataclass(frozen=True)
class Account:
    login: str
    id: int


@dataclass
class AppInstallation:
    id: int
    account: str
    repos: set[str]
    permissions: dict
    suspended: bool = False
    app: str = "workers"  # the App installed: the workers' (app_id) or the Curator's (curator_app_id)


@dataclass
class FakeRepo:
    """A repo of the fake: its default branch, the commit each branch points at, and its rulesets, each {id,
    enforcement, rules: [type], bypass: {app}} applying to the default branch."""

    owner: str
    name: str
    default_branch: str = "main"
    heads: dict = field(default_factory=dict)
    rulesets: list = field(default_factory=list)
    files: dict = field(default_factory=dict)  # branch -> the files of its pull request, as GitHub lists them


@dataclass
class FakePull:
    number: int
    owner: str
    name: str
    head: str
    head_sha: str
    base: str
    state: str = "open"
    merged: bool = False
    mergeable: bool | None = True
    merge_sha: str | None = None


@dataclass
class AppToken:
    installation: int
    repositories: tuple[str, ...]
    permissions: dict
    expires_at: float  # epoch seconds
    revoked: bool = False


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    headers: dict
    body: str


@dataclass
class _Device:
    account: Account
    steps: list[str]  # answers before the token: authorization_pending, slow_down, or a final error
    interval: int


@dataclass
class _Code:
    account: Account
    client_id: str
    redirect_uri: str
    challenge: str


@dataclass
class FakeGitHub:
    client_id: str = "Iv1.fake0client0id"
    client_secret: str = field(default_factory=lambda: "fake-client-secret-" + secrets.token_hex(16))
    device_account: Account | None = None
    device_steps: list[str] = field(default_factory=list)
    interval: int = 0  # seconds the device flow asks the CLI to wait between polls
    browser_account: Account | None = None  # who is signed in to github.com in the browser of the web flow
    status: int | None = None  # answer every request with this status instead
    delay: float = 0.0  # seconds to wait before answering
    requests: list[Recorded] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)  # token -> (Account, client id it was issued to, or None)
    codes: dict = field(default_factory=dict)  # web flow code -> _Code
    device_codes: dict = field(default_factory=dict)  # device code -> _Device
    issued_codes: list[str] = field(default_factory=list)
    app_id: str = "424242"  # the GitHub App's ID, which a JWT's iss must name
    app_public_key: str | None = None  # PEM the App's JWTs are checked with; None turns every JWT down
    curator_app_id: str = "434343"  # the Curator's App
    curator_app_public_key: str | None = None
    repos: dict = field(default_factory=dict)  # (owner, name), lower case -> FakeRepo
    pulls: dict = field(default_factory=dict)  # (owner, name, number), lower case names -> FakePull
    check_runs: dict = field(default_factory=dict)  # sha -> [check run]
    statuses: dict = field(default_factory=dict)  # sha -> combined status
    merges: list = field(default_factory=list)  # (owner, name, number, sha, app) of each merge
    installations: dict = field(default_factory=dict)  # installation id -> AppInstallation
    app_tokens: dict = field(default_factory=dict)  # installation token -> AppToken
    app_jwts: list[str] = field(default_factory=list)  # every App JWT a request carried, taken or not
    accounts: dict = field(default_factory=dict)  # login, lower case -> GitHub id, of every account the fake knows
    collaborators: dict = field(default_factory=dict)  # (owner, repo, login), lower case -> role on the repo

    def __post_init__(self):
        self._lock = threading.Lock()
        self._server = _QuietServer(("127.0.0.1", 0), _handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeGitHub:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()

    def issue_token(self, account: Account, *, app: bool = True) -> str:
        """A GitHub token of ``account``; ``app`` says whether this app issued it, as /applications checks."""
        token = "gho_" + secrets.token_hex(18)
        with self._lock:
            self.tokens[token] = (account, self.client_id if app else None)
            self.accounts[account.login.lower()] = account.id
        return token

    def collaborate(self, owner: str, repo: str, account: Account, role: str = "write") -> None:
        """Give ``account`` ``role`` (admin, maintain, write, triage or read) on owner/repo, as its admin would."""
        with self._lock:
            self.accounts[account.login.lower()] = account.id
            self.collaborators[(owner.lower(), repo.lower(), account.login.lower())] = role

    def install(self, account: str, *repos: str, permissions: dict | None = None, app: str = "workers") -> int:
        """Install the fake's GitHub App (``app``: workers or curator) on ``account`` for ``repos``; its id."""
        with self._lock:
            installation_id = 1001 + len(self.installations)
            granted = dict(APP_PERMISSIONS if permissions is None else permissions)
            self.installations[installation_id] = AppInstallation(
                installation_id, account, set(repos), granted, app=app
            )
        return installation_id

    # The Curator's repos

    def add_repo(
        self, owner: str, name: str, *, default_branch: str = "main", rulesets: list | None = None
    ) -> FakeRepo:
        repo = FakeRepo(owner, name, default_branch, {default_branch: "0" * 40}, list(rulesets or []))
        with self._lock:
            self.repos[(owner.lower(), name.lower())] = repo
        return repo

    def push(self, owner: str, name: str, branch: str, sha: str, files: list | None = None) -> None:
        """``branch`` of owner/name now points at ``sha``, and a pull request of it lists ``files``."""
        with self._lock:
            repo = self.repos[(owner.lower(), name.lower())]
            repo.heads[branch] = sha
            if files is not None:
                repo.files[branch] = files
            for found in self.pulls.values():
                if (found.owner, found.name, found.head) == (repo.owner, repo.name, branch) and found.state == "open":
                    found.head_sha = sha

    def ci(self, sha: str, runs: list | None = None, statuses: dict | None = None) -> None:
        """What CI says of ``sha``: its check runs (name, status, conclusion) and its combined status."""
        with self._lock:
            self.check_runs[sha] = [dict(item) for item in runs or []]
            if statuses is not None:
                self.statuses[sha] = statuses

    def pull_of(self, owner: str, name: str, branch: str) -> FakePull | None:
        return next(
            (item for item in self.pulls.values() if (item.owner, item.name, item.head) == (owner, name, branch)),
            None,
        )

    def covers(self, token: str, owner: str, repo: str) -> bool:
        """Whether installation ``token`` opens owner/repo now: not revoked, not expired, and made for it."""
        with self._lock:
            found = self.app_tokens.get(token)
            if found is None or found.revoked or found.expires_at <= time.time():
                return False
            installation = self.installations[found.installation]
        same = owner.lower() == installation.account.lower()
        return same and repo.lower() in {name.lower() for name in found.repositories}

    def secrets(self) -> set[str]:
        """Every value the fake handed out or holds that must never reach a hub log."""
        with self._lock:
            return {
                self.client_secret,
                *self.tokens,
                *self.issued_codes,
                *self.device_codes,
                *self.app_tokens,
                *self.app_jwts,
            }

    def calls(self, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.path == path]

    # Endpoints: (status, JSON body) or (status, None, headers) for a redirect.

    def _device_code(self, form: dict):
        if form.get("client_id") != self.client_id:
            return 404, {"error": "Not Found"}
        if self.device_account is None:
            return 400, {"error": "device_flow_disabled"}
        code = "dc_" + secrets.token_hex(16)
        with self._lock:
            self.device_codes[code] = _Device(self.device_account, list(self.device_steps), self.interval)
        return 200, {
            "device_code": code,
            "user_code": "WDJB-MJHT",
            "verification_uri": f"{self.url}/login/device",
            "expires_in": 900,
            "interval": self.interval,
        }

    def _access_token(self, form: dict):
        if form.get("grant_type") == DEVICE_GRANT:
            device = self.device_codes.get(form.get("device_code", ""))
            if form.get("client_id") != self.client_id or device is None:
                return 200, {"error": "incorrect_device_code"}
            if device.steps:
                step = device.steps[0] if device.steps[0] in ("expired_token", "access_denied") else device.steps.pop(0)
                if step == "slow_down":
                    device.interval += 5
                    return 200, {"error": "slow_down", "interval": device.interval}
                return 200, {"error": step}
            return 200, {"access_token": self.issue_token(device.account), "token_type": "bearer", "scope": "read:user"}
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return 200, {"error": "incorrect_client_credentials"}
        with self._lock:
            code = self.codes.pop(form.get("code", ""), None)
        if code is None:
            return 200, {"error": "bad_verification_code"}
        if form.get("redirect_uri") != code.redirect_uri:
            return 200, {"error": "redirect_uri_mismatch"}
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != code.challenge:
            return 200, {"error": "bad_verification_code"}
        return 200, {"access_token": self.issue_token(code.account), "token_type": "bearer", "scope": "read:user"}

    def _authorize(self, query: dict):
        if query.get("client_id") != self.client_id or query.get("code_challenge_method") != "S256":
            return 400, {"error": "bad request"}
        if self.browser_account is None:
            return 403, {"error": "nobody is signed in to the fake github.com"}
        code = secrets.token_hex(10)
        with self._lock:
            redirect_uri, challenge = query["redirect_uri"], query["code_challenge"]
            self.codes[code] = _Code(self.browser_account, self.client_id, redirect_uri, challenge)
            self.issued_codes.append(code)
        location = query["redirect_uri"] + "?" + urlencode({"code": code, "state": query.get("state", "")})
        return 302, None, {"Location": location}

    def _user(self, headers: dict):
        scheme, _, token = headers.get("authorization", "").partition(" ")
        found = self.tokens.get(token) if scheme.lower() in ("bearer", "token") else None
        if found is None:
            return 401, {"message": "Bad credentials"}
        account = found[0]
        return 200, {"login": account.login, "id": account.id, "type": "User"}

    def _check_token(self, client_id: str, headers: dict, body: str):
        scheme, _, encoded = headers.get("authorization", "").partition(" ")
        given = base64.b64decode(encoded).decode() if scheme.lower() == "basic" else ""
        if client_id != self.client_id or given != f"{self.client_id}:{self.client_secret}":
            return 401, {"message": "Bad credentials"}
        token = json.loads(body or "{}").get("access_token")
        found = self.tokens.get(token)
        if found is None or found[1] != client_id:
            return 404, {"message": "Not Found"}
        account = found[0]
        return 200, {"app": {"client_id": client_id}, "user": {"login": account.login, "id": account.id}}

    def _jwt_refusal(self, headers: dict) -> str | None:
        """Why GitHub would turn down the App JWT of a request, or None when it takes it: RS256 signed by the key
        of ``app_public_key`` (``curator_app_public_key`` for the Curator's App), iss the App's ID, iat not ahead, exp
        in the future and at most 10 minutes away."""
        refusal, _ = self._jwt_app(headers)
        return refusal

    def _jwt_app(self, headers: dict) -> tuple[str | None, str | None]:
        """(refusal, None), or (None, the App the JWT is of: workers or curator)."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        scheme, _, jwt = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or jwt.count(".") != 2:
            return "A JSON web token could not be decoded", None
        with self._lock:
            self.app_jwts.append(jwt)
        head, body, signature = jwt.split(".")
        try:
            claims = json.loads(_unb64(body))
        except ValueError:
            return "A JSON web token could not be decoded", None
        issuer = str(claims.get("iss")) if isinstance(claims, dict) else None
        curator = issuer == self.curator_app_id
        app, public = ("curator", self.curator_app_public_key) if curator else ("workers", self.app_public_key)
        if public is None:
            return "Integration not found", None
        try:
            header, claims = json.loads(_unb64(head)), json.loads(_unb64(body))
            key = serialization.load_pem_public_key(public.encode())
            key.verify(_unb64(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, InvalidSignature):
            return "A JSON web token could not be decoded", None
        if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(claims, dict):
            return "A JSON web token could not be decoded", None
        if str(claims.get("iss")) not in (self.app_id, self.curator_app_id):
            return "Integration not found", None
        now, issued, expires = time.time(), claims.get("iat"), claims.get("exp")
        if type(issued) is not int or issued > now + JWT_LEEWAY_SECONDS:
            refusal = "'Issued at' claim ('iat') must be an Integer representing the time that the assertion was issued"
            return refusal, None
        if type(expires) is not int or expires <= now:
            return "'Expiration time' claim ('exp') must be a numeric value representing the future time", None
        if expires > now + JWT_MAX_SECONDS:
            return "'Expiration time' claim ('exp') is too far in the future", None
        return None, app

    def _installation(self, owner: str, repo: str, headers: dict):
        refusal, app = self._jwt_app(headers)
        if refusal:
            return 401, {"message": refusal}
        with self._lock:
            for found in self.installations.values():
                names = {name.lower() for name in found.repos}
                if found.app == app and found.account.lower() == owner.lower() and repo.lower() in names:
                    return 200, {
                        "id": found.id,
                        "account": {"login": found.account, "type": "User"},
                        "repository_selection": "selected",
                        "permissions": dict(found.permissions),
                        "suspended_at": None,
                    }
        return 404, {"message": "Not Found"}

    def _access_tokens(self, installation_id: str, headers: dict, body: str):
        refusal, app = self._jwt_app(headers)
        if refusal:
            return 401, {"message": refusal}
        found = self.installations.get(int(installation_id)) if installation_id.isdigit() else None
        if found is None or found.app != app:
            return 404, {"message": "Not Found"}
        if found.suspended:
            return 403, {"message": "This installation has been suspended"}
        try:
            asked = json.loads(body or "{}")
        except ValueError:
            return 400, {"message": "Problems parsing JSON"}
        names = asked.get("repositories")
        names = sorted(found.repos) if names is None else names
        repos = {name.lower(): name for name in found.repos}
        if not names or any(name.lower() not in repos for name in names):
            return 422, {
                "message": "There is at least one repository that does not exist or is not accessible to the parent "
                "installation."
            }
        permissions = asked.get("permissions") or dict(found.permissions)
        for name, level in permissions.items():
            if _LEVELS.get(level, 99) > _LEVELS.get(found.permissions.get(name), 0):
                return 422, {"message": "The permissions requested are not granted to this installation."}
        token = "ghs_" + secrets.token_hex(18)
        expires = time.time() + APP_TOKEN_SECONDS
        covered = tuple(sorted(repos[name.lower()] for name in names))
        with self._lock:
            self.app_tokens[token] = AppToken(found.id, covered, dict(permissions), expires)
        return 201, {
            "token": token,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
            "permissions": permissions,
            "repository_selection": "selected",
            "repositories": [{"name": name, "full_name": f"{found.account}/{name}"} for name in covered],
        }

    def _permission(self, owner: str, repo: str, login: str, headers: dict):
        scheme, _, token = headers.get("authorization", "").partition(" ")
        with self._lock:
            found = self.app_tokens.get(token) if scheme.lower() in ("bearer", "token") else None
            if found is None or found.revoked or found.expires_at <= time.time():
                return 401, {"message": "Bad credentials"}
            installation = self.installations[found.installation]
            opens = owner.lower() == installation.account.lower() and repo.lower() in {
                name.lower() for name in found.repositories
            }
            account_id = self.accounts.get(login.lower())
            role = self.collaborators.get((owner.lower(), repo.lower(), login.lower()))
        if not opens or account_id is None:
            return 404, {"message": "Not Found"}
        if login.lower() == owner.lower():
            role = "admin"
        role = role or "none"
        return 200, {
            "permission": LEGACY_PERMISSIONS.get(role, "none"),
            "role_name": role,
            "user": {"login": login, "id": account_id, "type": "User"},
        }

    def _revoke_token(self, headers: dict):
        scheme, _, token = headers.get("authorization", "").partition(" ")
        with self._lock:
            found = self.app_tokens.get(token) if scheme.lower() in ("bearer", "token") else None
            if found is None or found.revoked or found.expires_at <= time.time():
                return 401, {"message": "Bad credentials"}
            found.revoked = True
        return 204, None

    # The Curator's repos, pull requests, CI and merges

    def _api_token(self, headers: dict, owner: str, name: str, permission: str, level: str):
        """(AppToken, app, None) for an installation token that opens owner/name and holds ``permission`` at
        ``level`` or more; else (None, None, (status, body))."""
        scheme, _, token = headers.get("authorization", "").partition(" ")
        with self._lock:
            found = self.app_tokens.get(token) if scheme.lower() in ("bearer", "token") else None
            if found is None or found.revoked or found.expires_at <= time.time():
                return None, None, (401, {"message": "Bad credentials"})
            installation = self.installations[found.installation]
        opens = owner.lower() == installation.account.lower() and name.lower() in {
            item.lower() for item in found.repositories
        }
        if not opens or (owner.lower(), name.lower()) not in self.repos:
            return None, None, (404, {"message": "Not Found"})
        if _LEVELS.get(found.permissions.get(permission), 0) < _LEVELS[level]:
            return None, None, (403, {"message": "Resource not accessible by integration"})
        return found, installation.app, None

    def _repo_answer(self, repo: FakeRepo) -> dict:
        return {"full_name": f"{repo.owner}/{repo.name}", "default_branch": repo.default_branch}

    def _pull_answer(self, pull: FakePull) -> dict:
        return {
            "number": pull.number,
            "state": pull.state,
            "merged": pull.merged,
            "mergeable": pull.mergeable,
            "html_url": f"https://github.com/{pull.owner}/{pull.name}/pull/{pull.number}",
            "head": {"ref": pull.head, "sha": pull.head_sha},
            "base": {"ref": pull.base},
        }

    def _blocks(self, repo: FakeRepo, branch: str, app: str | None) -> list[dict]:
        """The active rulesets with an update rule on ``branch`` (the default branch) that ``app`` may not bypass."""
        if branch != repo.default_branch:
            return []
        return [
            item
            for item in repo.rulesets
            if item.get("enforcement") == "active"
            and "update" in item.get("rules", ())
            and app not in item.get("bypass", ())
        ]

    def _curator_api(self, method: str, parts: list[str], query: dict, headers: dict, body: str):
        owner, name, rest = parts[1], parts[2], parts[3:]
        repo = self.repos.get((owner.lower(), name.lower()))

        def need(permission: str, level: str):
            return self._api_token(headers, owner, name, permission, level)

        if method == "GET" and not rest:
            _, _, refused = need("metadata", "read")
            return refused or (200, self._repo_answer(repo))
        if method == "GET" and rest[:2] == ["rules", "branches"]:
            _, _, refused = need("metadata", "read")
            if refused:
                return refused
            branch = "/".join(rest[2:])
            rules = []
            for item in repo.rulesets if branch == repo.default_branch else []:
                if item.get("enforcement") in ("active", "evaluate"):
                    rules += [
                        {"type": rule, "ruleset_id": item["id"], "ruleset_source_type": "Repository"}
                        for rule in item.get("rules", ())
                    ]
            return 200, rules
        if method == "GET" and rest[:1] == ["rulesets"] and len(rest) == 2:
            _, app, refused = need("metadata", "read")
            if refused:
                return refused
            found = next((item for item in repo.rulesets if str(item["id"]) == rest[1]), None)
            if found is None:
                return 404, {"message": "Not Found"}
            answer = {
                "id": found["id"],
                "name": found.get("name", "default branch"),
                "enforcement": found.get("enforcement", "active"),
                "rules": [{"type": rule} for rule in found.get("rules", ())],
            }
            if not found.get("hide_bypass"):
                answer["current_user_can_bypass"] = "always" if app in found.get("bypass", ()) else "never"
            return 200, answer
        if rest == ["pulls"] and method == "POST":
            _, _, refused = need("pull_requests", "write")
            if refused:
                return refused
            asked = json.loads(body or "{}")
            branch, base = asked.get("head"), asked.get("base")
            if branch not in repo.heads:
                return 422, {"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]}
            if any(item.head == branch and item.state == "open" for item in self.pulls.values()):
                return 422, {"message": "A pull request already exists"}
            number = 1 + sum(1 for key in self.pulls if key[:2] == (owner.lower(), name.lower()))
            pull = FakePull(number, repo.owner, repo.name, branch, repo.heads[branch], base)
            with self._lock:
                self.pulls[(owner.lower(), name.lower(), number)] = pull
            return 201, self._pull_answer(pull)
        if rest == ["pulls"] and method == "GET":
            _, _, refused = need("pull_requests", "read")
            if refused:
                return refused
            branch = (query.get("head") or "").partition(":")[2]
            found = [
                self._pull_answer(item)
                for item in self.pulls.values()
                if item.head == branch and item.state == query.get("state", "open")
            ]
            return 200, found
        if rest[:1] == ["pulls"] and len(rest) >= 2 and rest[1].isdigit():
            pull = self.pulls.get((owner.lower(), name.lower(), int(rest[1])))
            if pull is None:
                return 404, {"message": "Not Found"}
            if method == "GET" and len(rest) == 2:
                _, _, refused = need("pull_requests", "read")
                return refused or (200, self._pull_answer(pull))
            if method == "GET" and rest[2:] == ["files"]:
                _, _, refused = need("pull_requests", "read")
                if refused:
                    return refused
                page = int(query.get("page", "1"))
                files = repo.files.get(pull.head, [])
                return 200, files[(page - 1) * 100 : page * 100]
            if method == "PUT" and rest[2:] == ["merge"]:
                _, app, refused = need("contents", "write")
                if refused:
                    return refused
                asked = json.loads(body or "{}")
                if pull.state != "open":
                    return 405, {"message": "Pull Request is not mergeable"}
                if asked.get("sha") != pull.head_sha:
                    return 409, {"message": "Head branch was modified. Review and try the merge again."}
                if self._blocks(repo, pull.base, app):
                    return 405, {"message": "Repository rule violations found"}
                merged = secrets.token_hex(20)
                with self._lock:
                    pull.state, pull.merged, pull.merge_sha = "closed", True, merged
                    repo.heads[pull.base] = merged
                    self.merges.append((repo.owner, repo.name, pull.number, asked.get("sha"), app))
                return 200, {"sha": merged, "merged": True, "message": "Pull Request successfully merged"}
        if method == "GET" and rest[:1] == ["commits"] and len(rest) == 3 and rest[2] == "check-runs":
            _, _, refused = need("checks", "read")
            if refused:
                return refused
            found = self.check_runs.get(rest[1], [])
            return 200, {"total_count": len(found), "check_runs": found}
        if method == "GET" and rest[:1] == ["commits"] and len(rest) == 3 and rest[2] == "status":
            _, _, refused = need("statuses", "read")
            if refused:
                return refused
            return 200, self.statuses.get(rest[1], {"state": "pending", "total_count": 0, "statuses": []})
        if method == "POST" and rest == ["check-runs"]:
            _, app, refused = need("checks", "write")
            if refused:
                return refused
            asked = json.loads(body or "{}")
            check_id = 9000 + sum(len(items) for items in self.check_runs.values())
            made = {**asked, "id": check_id, "app": {"slug": f"evo-agents-{app}"}}
            with self._lock:
                self.check_runs.setdefault(asked["head_sha"], []).append(made)
            return 201, made
        return 404, {"message": "Not Found"}

    def answer(self, method: str, path: str, query: dict, headers: dict, body: str):
        self.requests.append(Recorded(method, path, query, headers, body))
        if self.delay:
            time.sleep(self.delay)
        if self.status is not None:
            return self.status, {"message": "unavailable"}
        form = {k: v[0] for k, v in parse_qs(body).items()} if method == "POST" else {}
        if method == "POST" and path == "/login/device/code":
            return self._device_code(form)
        if method == "POST" and path == "/login/oauth/access_token":
            return self._access_token(form)
        if method == "GET" and path == "/login/oauth/authorize":
            return self._authorize(query)
        if method == "GET" and path == "/user":
            return self._user(headers)
        if method == "POST" and path.startswith("/applications/") and path.endswith("/token"):
            return self._check_token(path.split("/")[2], headers, body)
        parts = path.strip("/").split("/")
        if method == "GET" and parts[0] == "repos" and parts[3:] == ["installation"]:
            return self._installation(parts[1], parts[2], headers)
        permission = len(parts) == 6 and (parts[0], parts[3], parts[5]) == ("repos", "collaborators", "permission")
        if method == "GET" and permission:
            return self._permission(parts[1], parts[2], parts[4], headers)
        if method == "POST" and parts[:2] == ["app", "installations"] and parts[3:] == ["access_tokens"]:
            return self._access_tokens(parts[2], headers, body)
        if method == "DELETE" and path == "/installation/token":
            return self._revoke_token(headers)
        if parts[0] == "repos" and len(parts) >= 3 and (parts[1].lower(), parts[2].lower()) in self.repos:
            return self._curator_api(method, parts, query, headers, body)
        if path == "/moved":
            return 302, None, {"Location": f"{self.url}/user"}
        return 404, {"message": "Not Found"}


def _unb64(text: str) -> bytes:
    """A part of a JWT, base64url without padding, decoded; ValueError when it is not one."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a client that timed out and hung up is expected here
        pass


def _handler(fake: FakeGitHub):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload, *extra = fake.answer(method, parts.path, query, headers, body)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            for name, value in (extra[0] if extra else {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # the names http.server calls
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

        def do_PUT(self):
            self._serve("PUT")

        def log_message(self, format, *args):  # keep the test output quiet
            pass

    return Handler
