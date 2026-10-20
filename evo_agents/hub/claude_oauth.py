"""Claude's OAuth sign-in for ``evo-agents hub account oauth-login``: the authorization-code flow with PKCE that Claude
Code itself and CLIProxyAPIPlus's ``--claude-login`` run, on the member's machine.

``authorize_url`` is the page of claude.ai the member approves on; claude.ai then sends the browser back to
``http://localhost:54545/callback?code=...&state=...``, which ``wait_for_callback`` answers, or the member pastes what
the page showed (``parse_code``). ``exchange`` trades the code, with the PKCE verifier, for the tokens at the token
endpoint: an access token, a refresh token, how long the access token lives, and who the account is. Nothing is
written to a file: the verifier, the code and the tokens live in memory until the command puts them on the hub, and no
message, not even an error, repeats one.

The endpoints and the client id are those Claude Code uses; ``EVO_CLAUDE_OAUTH_AUTHORIZE_URL`` and
``EVO_CLAUDE_OAUTH_TOKEN_URL`` name others (a test's fake endpoint). Standard library only, like the rest of the
client.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 54545
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
SCOPES = "org:create_api_key user:profile user:inference"
TIMEOUT_S = 30
WAIT_S = 300.0  # how long the member has to approve in the browser


class OAuthError(Exception):
    """The sign-in failed; ``str`` says why, never with a code or a token."""


class ListenError(OAuthError):
    """This machine cannot listen for the browser's answer: the member pastes it instead."""


@dataclass(frozen=True)
class Tokens:
    """What the token endpoint gave. ``repr`` shows no token."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime
    account_uuid: str | None = None
    organization_uuid: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str
    state: str


def new_pkce() -> Pkce:
    """A verifier, its S256 challenge and a state, drawn at random."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return Pkce(verifier, challenge, secrets.token_urlsafe(24))


def authorize_url(pkce: Pkce) -> str:
    base = os.environ.get("EVO_CLAUDE_OAUTH_AUTHORIZE_URL") or AUTHORIZE_URL
    query = urllib.parse.urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "state": pkce.state,
        }
    )
    return f"{base}?{query}"


def parse_code(text: str, state: str) -> str:
    """The authorization code in what the member pasted: the callback URL, ``code#state`` as the page of claude.ai shows
    it, or the bare code. OAuthError for nothing, or for a state that is not this sign-in's."""
    text = text.strip()
    if not text:
        raise OAuthError("no code: paste the code, or the address the browser went to after you approved")
    got_state = None
    if "?" in text or text.startswith(("http://", "https://")):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
        code = (query.get("code") or [""])[0]
        got_state = (query.get("state") or [None])[0]
    elif "#" in text:
        code, _, got_state = text.partition("#")
    else:
        code = text
    if not code or any(char.isspace() for char in code):
        raise OAuthError("found no authorization code in what was pasted")
    if got_state is not None and not secrets.compare_digest(got_state, state):
        raise OAuthError("the state of the answer is not this sign-in's: start `oauth-login` again")
    return code


def wait_for_callback(state: str, timeout: float = WAIT_S, port: int = CALLBACK_PORT) -> str:
    """Listen on localhost for claude.ai's redirect and give its code; OAuthError when the port is taken, the answer
    is another sign-in's or refused, or ``timeout`` seconds pass. Needs a browser on this machine."""
    found: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (the name http.server calls)
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != CALLBACK_PATH:
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(parts.query)
            for key in ("code", "state", "error"):
                if key in query:
                    found[key] = query[key][0]
            ok = "code" in found and "error" not in found
            page = "You can close this tab and go back to the terminal." if ok else "Sign-in refused."
            body = f"<!doctype html><title>evo-agents</title><p>{page}</p>".encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # nothing of the request is logged: its query holds the code
            return

    try:
        server = HTTPServer((CALLBACK_HOST, port), Handler)
    except OSError as exc:
        raise ListenError(f"cannot listen on localhost:{port} for the answer of claude.ai: {exc.strerror}") from None
    server.timeout = 0.5
    deadline = time.monotonic() + timeout
    try:
        while not found and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not found:
        raise OAuthError("no answer from the browser in time")
    if "error" in found or "code" not in found:
        raise OAuthError(f"claude.ai refused the sign-in ({found.get('error', 'no code')})")
    if not secrets.compare_digest(found.get("state", ""), state):
        raise OAuthError("the state of the answer is not this sign-in's: start `oauth-login` again")
    return found["code"]


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "evo-agents"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (https, or a test's)
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"the token endpoint refused the code (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthError(f"cannot reach the token endpoint: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise OAuthError("the token endpoint answered with something that is not JSON") from None


def tokens_from(answer: dict, now: datetime) -> Tokens:
    """``Tokens`` from the token endpoint's answer; OAuthError when it lacks an access token, a refresh token or a
    lifetime."""
    access, refresh, lifetime = answer.get("access_token"), answer.get("refresh_token"), answer.get("expires_in")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise OAuthError("the token endpoint gave no access token and refresh token")
    if isinstance(lifetime, bool) or not isinstance(lifetime, int | float) or lifetime <= 0:
        raise OAuthError("the token endpoint gave no lifetime for the access token")
    account = answer.get("account") if isinstance(answer.get("account"), dict) else {}
    org = answer.get("organization") if isinstance(answer.get("organization"), dict) else {}
    return Tokens(
        access,
        refresh,
        now + timedelta(seconds=lifetime),
        account.get("uuid"),
        org.get("uuid"),
        account.get("email_address"),
    )


def exchange(code: str, pkce: Pkce, now: datetime | None = None) -> Tokens:
    """The tokens ``code`` buys at the token endpoint."""
    url = os.environ.get("EVO_CLAUDE_OAUTH_TOKEN_URL") or TOKEN_URL
    answer = _post(
        url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "state": pkce.state,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": pkce.verifier,
        },
    )
    return tokens_from(answer, now or datetime.now(UTC))
