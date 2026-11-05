"""Hub credentials and the check every /v1 request goes through.

A machine token is ``evh_``, a web session ``evs_``, the token of one worker ``evw_`` and the token of one run that runs
as its owner ``evr_`` (``evo_agents.hub.server.run_access``), each followed by 32 random bytes in base64url. Postgres
keeps only the hex SHA-256 of any of them, in ``tokens`` with its kind; 32 random bytes need no key, while a pairing
code, which is short, is kept as a ``keyed_digest`` under the session secret. The plaintext exists only in the response
that issues it and on the holder's side. A token expires after TOKEN_TTL without use: using it pushes the expiry
TOKEN_TTL past now, but the row is written at most once per TOUCH_EVERY, so a busy token does not write on every
request. Revoked, expired and unknown credentials all get the same 401, which names
``evo-agents hub login`` (``evo-agents worker join`` on the worker's routes).

``Authenticate`` guards every path under /v1 except PUBLIC_PATHS and the paths under PUBLIC_PREFIXES, and fails
closed: a route added later needs a credential unless it is listed there. Each public prefix checks a credential of
its own. One serves the files of plugins' pages to their sandboxed frames, whose requests carry no cookie: a ticket in
the path is their credential (``evo_agents.hub.server.plugin_pages``). Another is the API of the hub for plugins,
whose credential is a plugin token, ``Authorization: Bearer evp_...`` (``evo_agents.hub.server.task_dispatch``). The
third, HOOKS_PREFIX, holds the webhooks of core plugins, each under ``/v1/hooks/<its id>/``
(``evo_agents.hub.server.plugin_routes``): a service outside the hub calls them with a secret of the plugin's, which
the route compares itself; the request has no principal, so a route there that asks for the caller gets 401. The
fourth, SELLER_PREFIX, is the API a seller's hub serves the hubs that buy its plugins
(``evo_agents.hub.server.seller_api``): an order's secret or a licence the seller signed is the credential, which the
route checks itself, as it checks that the feature seller is on. A machine token arrives as ``Authorization: Bearer`` and a web session as
the SESSION_COOKIE cookie; each is accepted only through its own channel. A write (POST, PUT, PATCH, DELETE) made
with the cookie must also carry X-Evo-CSRF, an HMAC of the session under the session secret that GET
/v1/auth/web/csrf hands out; a Bearer request needs none, since a browser never adds that header by itself.
Every comparison of a secret value is constant-time.

An ingest key of a project's signals, ``evi_...`` (``evo_agents.hub.signals``), arrives as ``Authorization: Bearer``
and passes only to the one route it writes through, ``POST /v1/signals/{project}`` (INGEST_ROUTE), which checks the key
itself; anywhere else it gets 403 before the database is asked, the routes that read signals included. That route also
takes a member's machine token or web session, checked here as anywhere.

A worker token arrives as ``Authorization: Bearer`` too, and works only under WORKER_PREFIX, the routes of the
worker daemon, where machine tokens and web sessions get 403: neither side can stand in for the other, and the
check happens before the database is asked. Every request under WORKER_PREFIX, the public /v1/worker/join included,
carries ``X-Evo-Worker-Protocol: 1`` (``evo_agents.hub.runs``); any other value, or none, gets 426 so an old daemon
learns it must be upgraded. A worker's principal is never a hub admin, whoever owns the worker. Outside /v1, the
hub's /mcp takes a worker token too, for the agent of one run its worker holds (``evo_agents.hub.server.mcp``): that
principal carries a ``RunScope``, and ``evo_agents.hub.server.projects.project_access`` holds every read and write
to it.

A run's token arrives as ``Authorization: Bearer`` and is its owner, with the owner's grants on every project and hub
admin when the owner is one, while its lease is out (``run_access``): the run is held by the worker it was leased to,
its owner lets it run as them and still holds writer on its project, and neither the token nor its worker is revoked;
otherwise it gets the same 401 as any other credential. It never works under WORKER_PREFIX, is refused the paths of
``run_access_rules`` with 403 and the reason, but those of the groups its owner's run reach opens, which the statement
that finds the token reads with it (``Principal.run_reach``), and every write made with it, refused or not, adds an
audit row naming the run. While it is answered, ``run_access_rules.ACTING_RUN`` names the run, so no run starts from
it; its principal acts for the run (``Principal.acts_for_run``) as the agent of a run on /mcp does, for the rules of
the routes open to it; and a stream it opened looks again at each turn whether it is still live (``still_live``). The
token of a KG run (``kg_run_access``, host ``kg-run:<run id>``) lives the same way without its run running as its
owner, is never a hub admin, and reaches the routes of ``kg_run_access.ROUTES`` on its run's project alone. The
gateway route's token of a run (``gateway_leases``) shares the prefix and is a credential of those routes alone: here
it is unknown.

The middleware reads credentials of HTTP requests only. A websocket under /v1 reaches the app only when its path is
one of SELF_CHECKED_WEBSOCKETS, whose routes check their own credential (the two ends of the web terminal, see
``evo_agents.hub.server.terminal``, and of the live view of a run's browser, ``evo_agents.hub.server.browser_view``);
any other is closed during its handshake, which the client sees as a 403, so a websocket route added later is
refused until it checks a credential and is listed there.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import Depends, HTTPException, Request
from sqlalchemy import and_, bindparam, case, exists, func, insert, or_, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import Response
from starlette.websockets import WebSocketClose

from evo_agents.hub import tables
from evo_agents.hub.access import ROLES, has_role
from evo_agents.hub.runs import HELD_STATES, PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.hub.server import kg_run_access, run_access_rules
from evo_agents.hub.server.errors import error_response

log = logging.getLogger(__name__)

MACHINE = "machine"
WEB = "web"
WORKER = "worker"
RUN = "run"  # the token of a run that runs as its owner (``run_access``)
PREFIXES = {MACHINE: "evh_", WEB: "evs_", WORKER: "evw_", RUN: "evr_"}
TOKEN_BYTES = 32
TOKEN_TTL = timedelta(days=90)  # without use; every use moves the expiry
TOUCH_EVERY = timedelta(days=1)  # at most one write of last_used_at and expires_at per token in this time
SESSION_COOKIE = "evo_hub_session"
CSRF_HEADER = "X-Evo-CSRF"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PROTECTED_PREFIX = "/v1/"
WORKER_PREFIX = "/v1/worker/"  # the worker daemon's routes: a worker token works there and nowhere else
PUBLIC_PATHS = frozenset(
    {
        "/v1/health",
        "/v1/health/live",
        "/v1/openapi.json",
        "/v1/auth/config",
        "/v1/auth/github",
        "/v1/auth/web/login",
        "/v1/auth/web/callback",
        "/v1/worker/join",  # a pairing code or a pool's join token is the credential
        "/v1/invites/preview",  # an invite link's token is the credential; it shows what the link gives, never more
        # the secret token Telegram sends is the credential: the named exception of the core plugin telegram, whose
        # webhook kept its path when the other core plugins' went under HOOKS_PREFIX
        "/v1/telegram/webhook",
        "/v1/branding",  # the sign-in page shows the hub's branding (evo_agents.hub.server.branding)
        "/v1/branding/logo",
        "/v1/branding/favicon",
        "/v1/push/config",  # the public VAPID key a browser subscribes with, and which push senders are on
        # the gateway route of runs on a provider account's model: a run's token is the credential (gateway_proxy)
        "/v1/messages",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/models",
    }
)
HOOKS_PREFIX = "/v1/hooks/"  # the webhooks of core plugins, /v1/hooks/<plugin id>/... (plugin_routes)
SELLER_PREFIX = "/v1/seller-api/"  # what a seller's hub serves buying hubs (seller_api)
# Paths under these prefixes check their own credential: a ticket in the path (evo_agents.hub.server.plugin_pages), a
# plugin token (evo_agents.hub.server.task_dispatch), a core plugin's own secret (HOOKS_PREFIX), or an order's secret
# or a licence (SELLER_PREFIX).
PUBLIC_PREFIXES = ("/v1/plugin-ui/", "/v1/plugin-api/", HOOKS_PREFIX, SELLER_PREFIX)
# Websockets under /v1 whose routes check their own credential; every other one is closed before the app sees it.
SELF_CHECKED_WEBSOCKETS = (
    re.compile(r"/v1/projects/[^/]+/runs/[^/]+/terminal"),  # the browser's end: the session cookie, Origin, CSRF
    re.compile(r"/v1/worker/runs/[^/]+/terminal"),  # the worker's end: its evw_ token and the protocol header
    re.compile(r"/v1/projects/[^/]+/runs/[^/]+/browser"),  # the live view's viewer: as the terminal's browser end
    re.compile(r"/v1/worker/runs/[^/]+/browser"),  # the worker's end of the live view: as the terminal's
)
WEBSOCKET_REFUSED = 4403  # the close code of a websocket under /v1 that no route checks a credential of
INGEST_PREFIX = "evi_"  # an ingest key of a project's signals (evo_agents.hub.signals)
INGEST_ROUTE = re.compile(r"/v1/signals/[^/]+")  # where an ingest key writes, with POST, and nowhere else
LOGIN_HINT = "run `evo-agents hub login`"
JOIN_HINT = "join the machine with `evo-agents worker join`"
WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="evo-agents hub"'}

_TOKEN = re.compile(r"ev[hswr]_[A-Za-z0-9_-]{43}")  # a prefix and 32 bytes of base64url without padding
WRITER_ROLES = tuple(role for role in ROLES if has_role(role, "writer"))


def admin_login(users=None):
    """The EVO_HUB_ADMINS login a users row answers to, as a column for ``HubConfig.is_admin``: the one bound to its
    GitHub account (users.admin_login, ``bind_admin``), which a rename on GitHub keeps, else its own login while no
    account holds that one bound. A login another account took since never makes its new holder an admin."""
    users = tables.users if users is None else users
    bound = tables.users.alias("bound")
    own = func.lower(users.c.login)
    taken = exists().where(bound.c.admin_login == own)
    return func.coalesce(users.c.admin_login, case((taken, None), else_=own))


async def bind_admin(conn: AsyncConnection, admins: frozenset[str], user_id: int, login: str) -> str | None:
    """Bind ``login``, when EVO_HUB_ADMINS lists it, to the account of row ``user_id`` (users.admin_login), unless
    another account holds it or this one holds a listed login already; the login the row answers to as an admin
    after (``admin_login``)."""
    users = tables.users
    listed = login.lower()
    if listed in admins:
        held = select(users.c.admin_login).where(users.c.id == user_id).with_for_update()
        if (await conn.execute(held)).scalar_one_or_none() not in admins:
            bound = tables.users.alias("bound")
            free = ~exists().where(bound.c.admin_login == listed)
            try:
                async with conn.begin_nested():
                    await conn.execute(update(users).values(admin_login=listed).where(users.c.id == user_id, free))
            except sa_exc.IntegrityError:  # another account bound it at the same moment: it is that one's
                pass
    return (await conn.execute(select(admin_login()).where(users.c.id == user_id))).scalar_one_or_none()


async def member_principal(conn: AsyncConnection, user_id: int, config) -> Principal:
    """Member ``user_id`` as the hub sees a request of theirs that carries no token: a hub admin when their account
    answers to a login of EVO_HUB_ADMINS (``admin_login``), never without the hub's config, which names them."""
    users = tables.users
    query = select(users.c.login, admin_login(users).label("admin_login")).where(users.c.id == user_id)
    row = (await conn.execute(query)).one()
    admin = config is not None and config.is_admin(row.admin_login)
    return Principal(user_id, row.login, admin, None, MACHINE, "", admin_login=row.admin_login)


async def bind_admins(engine, admins: frozenset[str]) -> None:
    """At the hub's start, bind each listed login to the account that signed in with it (``bind_admin``), so an admin
    who signed in before the hub bound logins keeps the role after a rename."""
    users = tables.users
    query = select(users.c.id, users.c.login).where(
        func.lower(users.c.login).in_(sorted(admins)), users.c.github_id.is_not(None)
    )
    async with engine.begin() as conn:
        for row in (await conn.execute(query)).all():
            await bind_admin(conn, admins, row.id, row.login)


def _authenticate():
    """One statement: find a live token of kind ``:token_kind`` with hash ``:digest``, and when it was last written more
    than TOUCH_EVERY ago, push its expiry and mark the user seen. Two requests racing over a stale token write once:
    the second UPDATE waits for the first, then finds last_used_at fresh and changes nothing. Built once
    (AUTHENTICATE): every request runs it, and a statement built per call costs more than its round trip."""
    tokens, users = tables.tokens, tables.users
    found = (
        select(tokens.c.id, tokens.c.user_id, users.c.login, admin_login(users).label("admin_login"))
        .join_from(tokens, users, users.c.id == tokens.c.user_id)
        .where(
            tokens.c.token_hash == bindparam("digest"),
            tokens.c.kind == bindparam("token_kind"),
            tokens.c.revoked_at.is_(None),
            tokens.c.expires_at > func.now(),
        )
        .cte("found")
    )
    touched = (
        update(tokens)
        .values(last_used_at=func.now(), expires_at=func.now() + TOKEN_TTL)
        .where(
            tokens.c.id == select(found.c.id).scalar_subquery(),
            or_(tokens.c.last_used_at.is_(None), tokens.c.last_used_at <= func.now() - TOUCH_EVERY),
        )
        .returning(tokens.c.id)
        .cte("touched")
    )
    seen = (
        update(users)
        .values(last_seen_at=func.now())
        .where(users.c.id == select(found.c.user_id).scalar_subquery(), exists(select(touched.c.id)))
        .returning(users.c.id)
        .cte("seen")
    )
    return select(found.c.id, found.c.user_id, found.c.login, found.c.admin_login).add_cte(touched, seen)


AUTHENTICATE = _authenticate()


def _live_run_token(match):
    """The live token of a run that ``match`` names, while its lease is out (``run_access``): the lease is not given
    back, the run is held by the worker it was leased to and its owner lets it run as them, or the token is a KG run's
    (``kg_run_access``, its host says so), the owner dispatched it and still holds writer on its project, and the worker
    is not revoked; with the owner's run reach (``run_access_rules.REACH_GROUPS``), whether it is a KG run's and the
    run's project."""
    tokens, users, cl, r, w, g, p = (
        tables.tokens,
        tables.users,
        tables.credential_leases,
        tables.runs,
        tables.workers,
        tables.grants,
        tables.projects,
    )
    kg = tokens.c.host == kg_run_access.host_of(r.c.id)
    return (
        select(
            tokens.c.id,
            tokens.c.user_id,
            users.c.login,
            admin_login(users).label("admin_login"),
            cl.c.run_id,
            users.c.run_reach,
            kg.label("kg"),
            p.c.name.label("project"),
        )
        .join_from(tokens, users, users.c.id == tokens.c.user_id)
        .join(cl, cl.c.token_id == tokens.c.id)
        .join(r, r.c.id == cl.c.run_id)
        .join(p, p.c.id == r.c.project_id)
        .join(w, w.c.id == cl.c.worker_id)
        .join(g, and_(g.c.project_id == r.c.project_id, g.c.user_id == tokens.c.user_id))
        .where(
            match,
            tokens.c.kind == RUN,
            tokens.c.revoked_at.is_(None),
            tokens.c.expires_at > func.now(),
            cl.c.revoked_at.is_(None),
            or_(r.c.as_owner.is_(True), kg),
            r.c.state.in_(HELD_STATES),
            r.c.worker_id == cl.c.worker_id,
            r.c.dispatched_by == tokens.c.user_id,
            w.c.revoked_at.is_(None),
            g.c.role.in_(WRITER_ROLES),
        )
    )


def _authenticate_run():
    """One statement: find the live token of a run, its hash ``:digest`` (``_live_run_token``), with its owner's run
    reach, and push its expiry as ``_authenticate`` does. The owner is not marked seen: the run is not them at their
    keyboard."""
    tokens = tables.tokens
    found = _live_run_token(tokens.c.token_hash == bindparam("digest")).cte("found")
    touched = (
        update(tokens)
        .values(last_used_at=func.now(), expires_at=func.now() + TOKEN_TTL)
        .where(
            tokens.c.id == select(found.c.id).scalar_subquery(),
            or_(tokens.c.last_used_at.is_(None), tokens.c.last_used_at <= func.now() - TOUCH_EVERY),
        )
        .returning(tokens.c.id)
        .cte("touched")
    )
    columns = (
        found.c.id,
        found.c.user_id,
        found.c.login,
        found.c.admin_login,
        found.c.run_id,
        found.c.run_reach,
        found.c.kg,
        found.c.project,
    )
    return select(*columns).add_cte(touched)


AUTHENTICATE_RUN = _authenticate_run()
RUN_TOKEN_LIVE = _live_run_token(tables.tokens.c.id == bindparam("token_id")).with_only_columns(tables.tokens.c.id)


@dataclass(frozen=True)
class RunScope:
    """What the agent of one run reaches with its worker's token: the run's project alone, with the owner's grant
    there capped at ``role`` and ``max_level``."""

    run_id: int
    project: str
    role: str  # the owner's role on the project when the request came, at most writer
    max_level: str  # the owner's grant's, when the request came
    kind: str = "step"  # the run's kind: a judge run's agent reads the code graph alone (``mcp_tools.JUDGE_TOOLS``)
    # What the memories of a run need (``memory_agents``): the profile and version it runs under, the handle of that
    # profile when it is not its project's default (whose agent writes into its own namespace), and its chat and hop.
    profile_id: int | None = None
    profile_version: int | None = None
    agent: str | None = None
    hop: int | None = None
    chat_id: int | None = None
    chat_message_id: int | None = None

    @property
    def namespace(self) -> int | None:
        """The profile whose memories the agent writes and reads besides the shared ones; None for a default
        profile, which writes the shared memories as before."""
        return self.profile_id if self.agent is not None else None


@dataclass(frozen=True)
class Principal:
    """Who made a request, and with which credential."""

    user_id: int
    login: str
    admin: bool  # it acts as a hub admin: its account answers to a login of EVO_HUB_ADMINS (admin_login)
    token_id: int
    kind: str  # MACHINE, WORKER or RUN (a Bearer token), or WEB (the session cookie)
    token_hash: str
    scope: RunScope | None = None  # the agent of a run, on /mcp only; None for everyone else
    run_id: int | None = None  # RUN: the run whose token it is, which acts as its owner; None for everyone else
    # RUN: the groups of run_access_rules.REACH_GROUPS its owner lets their runs reach (users.run_reach), read with the
    # token; empty for everyone else
    run_reach: frozenset[str] = frozenset()
    # the EVO_HUB_ADMINS login its account answers to (``admin_login``), whatever ``admin`` says of this credential
    admin_login: str | None = None
    # RUN of a KG run (``kg_run_access``): the one project it reaches, on the routes of ``kg_run_access.ROUTES`` alone;
    # None for everyone else
    kg_project: str | None = None

    def __repr__(self) -> str:
        run = self.scope.run_id if self.scope is not None else self.run_id
        shown = "" if run is None else f", run={run}"
        return f"Principal(login={self.login!r}, token_id={self.token_id}, kind={self.kind!r}{shown})"

    @property
    def acts_for_run(self) -> int | None:
        """The run whose agent makes the request: the run of a ``RunScope`` on /mcp, or the run of a run's token
        (RUN, ``run_access``) on the REST API; None for a member's own credential. A rule of the form "the agent of a
        run never does X" tests this, not ``scope``, which /mcp alone sets."""
        if self.scope is not None:
            return self.scope.run_id
        return self.run_id if self.kind == RUN else None

    @property
    def run_project(self) -> str | None:
        """The one project the agent of a run reaches (its scope's on /mcp, a KG run's token's on the REST API); None
        for any other principal, which reaches its grants'."""
        return self.kg_project if self.scope is None else self.scope.project

    def reaches(self, project: str) -> bool:
        """Whether a run's scope, or a KG run's token, leaves ``project`` within reach; always, without either."""
        return self.run_project is None or self.run_project == project


@dataclass(frozen=True)
class Issued:
    token: str  # the plaintext, handed to the caller once and never stored
    token_id: int
    expires_at: datetime

    def __repr__(self) -> str:
        return f"Issued(token_id={self.token_id})"


def new_token(kind: str) -> str:
    return PREFIXES[kind] + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(given: str, expected: str) -> bool:
    """Constant-time equality of two strings, any characters."""
    return hmac.compare_digest(given.encode(), expected.encode())


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, purpose: str, message: bytes) -> bytes:
    # The purpose keeps a value signed for one use from being accepted for another.
    return hmac.new(secret.encode(), purpose.encode() + b"\0" + message, hashlib.sha256).digest()


def keyed_digest(secret: str, purpose: str, text: str) -> str:
    """The hex HMAC-SHA256 of ``text`` under ``secret`` for ``purpose``: unlike a plain hash, nobody who reads it
    without the secret can test guesses of a short ``text`` against it."""
    return _mac(secret, purpose, text.encode()).hex()


def sign(secret: str, purpose: str, payload: dict, ttl: int) -> str:
    """``payload`` plus an expiry ``ttl`` seconds from now, as base64url JSON and its HMAC-SHA256. Readable by
    whoever holds the value; only the signature keeps it from being changed."""
    body = b64(json.dumps({**payload, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    return f"{body}.{b64(_mac(secret, purpose, body.encode()))}"


def unsign(secret: str, purpose: str, value: str) -> dict | None:
    """The payload ``sign`` wrote, or None when the value was changed, signed for another purpose, or expired."""
    body, dot, mac = value.partition(".")
    if not dot or not same(mac, b64(_mac(secret, purpose, body.encode()))):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int) or payload["exp"] < time.time():
        return None
    return payload


def csrf_token(secret: str, session_hash: str) -> str:
    """The X-Evo-CSRF value of one web session: it changes with the session and cannot be made without the secret."""
    return b64(_mac(secret, "csrf", session_hash.encode()))


def set_session_cookie(response: Response, token: str) -> None:
    max_age = int(TOKEN_TTL.total_seconds())
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def delete_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


async def issue_token(conn: AsyncConnection, user_id: int, kind: str, host: str | None) -> Issued:
    token = new_token(kind)
    tokens = tables.tokens
    inserted = await conn.execute(
        insert(tokens)
        .values(
            user_id=user_id,
            kind=kind,
            token_hash=hash_token(token),
            host=host,
            expires_at=func.now() + TOKEN_TTL,
        )
        .returning(tokens.c.id, tokens.c.expires_at)
    )
    token_id, expires_at = inserted.one()
    return Issued(token, token_id, expires_at)


async def revoke_token(conn: AsyncConnection, token_id: int, user_id: int) -> bool:
    """Revoke a token of ``user_id``; False when it has none by that id that is not revoked already."""
    tokens = tables.tokens
    revoked = await conn.execute(
        update(tokens)
        .values(revoked_at=func.now())
        .where(tokens.c.id == token_id, tokens.c.user_id == user_id, tokens.c.revoked_at.is_(None))
        .returning(tokens.c.id)
    )
    return revoked.one_or_none() is not None


async def authenticate(engine, token: str, kind: str, config) -> Principal | None:
    """The principal behind a live ``token`` of ``kind``, or None. A malformed value never reaches the database."""
    if not _TOKEN.fullmatch(token) or not token.startswith(PREFIXES[kind]):
        return None
    digest = hash_token(token)
    async with engine.begin() as conn:
        if kind == RUN:
            row = (await conn.execute(AUTHENTICATE_RUN, {"digest": digest})).one_or_none()
        else:
            row = (await conn.execute(AUTHENTICATE, {"digest": digest, "token_kind": kind})).one_or_none()
    if row is None:
        return None
    token_id, user_id, login, admin_as, *run = row
    admin = kind != WORKER and config.is_admin(admin_as)  # a worker acts for its owner on its own routes only
    if not run:
        return Principal(user_id, login, admin, token_id, kind, digest, admin_login=admin_as)
    run_id, reach, kg, project = run
    if kg:  # a KG run's token: its project's KG routes alone, never as a hub admin (``kg_run_access``)
        return Principal(user_id, login, False, token_id, kind, digest, run_id=run_id, kg_project=project)
    return Principal(
        user_id, login, admin, token_id, kind, digest, run_id=run_id, run_reach=frozenset(reach), admin_login=admin_as
    )


async def still_live(conn: AsyncConnection, principal: Principal) -> bool:
    """Whether the credential of ``principal`` still holds, for an answer that outlives the check at its start (a
    stream): a run's token (RUN) while its lease is out, as ``_authenticate_run`` finds it; any other credential is not
    looked at again here."""
    if principal.kind != RUN:
        return True
    return (await conn.execute(RUN_TOKEN_LIVE, {"token_id": principal.token_id})).first() is not None


def is_public(path: str) -> bool:
    """Whether a request to ``path`` needs no credential of the hub: PUBLIC_PATHS, and PUBLIC_PREFIXES."""
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def unauthorized(request: Request, message: str, *, clear_cookie: bool = False) -> Response:
    response = error_response(request, 401, message, headers=WWW_AUTHENTICATE)
    if clear_cookie:  # the browser should stop sending a session that will never work again
        delete_session_cookie(response)
    return response


class _Status:
    """The ``send`` of an ASGI app that remembers the status its answer started with."""

    def __init__(self, send):
        self.send = send
        self.status = 500  # an app that raised before it answered is answered 500

    async def __call__(self, message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        await self.send(message)


class Authenticate:
    """Pure ASGI middleware: every request under /v1 that is not public needs a live credential of a kind the path
    takes, a write made with the session cookie needs its CSRF header, and a request under WORKER_PREFIX needs the
    worker protocol header. The principal goes to ``request.state.principal``. A websocket under /v1 passes only to
    the routes of SELF_CHECKED_WEBSOCKETS, which check their own credential. A run's token is refused the paths of
    ``run_access_rules``, and each write made with it is audited once answered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "websocket" and path.startswith(PROTECTED_PREFIX):
            if any(pattern.fullmatch(path) for pattern in SELF_CHECKED_WEBSOCKETS):
                await self.app(scope, receive, send)
            else:  # fail closed: nothing here read a credential of it
                reason = "no websocket here checks a credential"
                await WebSocketClose(code=WEBSOCKET_REFUSED, reason=reason)(scope, receive, send)
            return
        worker_route = path.startswith(WORKER_PREFIX)
        if scope["type"] != "http" or not path.startswith(PROTECTED_PREFIX) or (is_public(path) and not worker_route):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        refusal = self._protocol_refusal(request) if worker_route else None
        if refusal is None and not is_public(path):
            refusal = await self._check(request, worker_route)
        answer = self.app if refusal is None else refusal
        principal = getattr(request.state, "principal", None)
        if principal is None or principal.kind != RUN:
            await answer(scope, receive, send)
            return
        acting = run_access_rules.ACTING_RUN.set(principal.run_id)  # no run starts while it answers
        try:
            if request.method not in UNSAFE_METHODS:
                await answer(scope, receive, send)
                return
            watched = _Status(send)
            try:
                await answer(scope, receive, watched)
            finally:
                engine = request.app.state.engine
                await run_access_rules.record_write(engine, principal, request.method, path, watched.status)
        finally:
            run_access_rules.ACTING_RUN.reset(acting)

    async def _check(self, request: Request, worker_route: bool) -> Response | None:
        config = request.app.state.config
        hint = JOIN_HINT if worker_route else LOGIN_HINT
        authorization = request.headers.get("authorization")
        if authorization is not None:  # an explicit header wins; a bad one never falls back to the cookie
            scheme, _, token = authorization.strip().partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return unauthorized(request, f"the Authorization header must be `Bearer <token>`: {hint}")
            token = token.strip()
            if token.startswith(INGEST_PREFIX):
                return self._ingest_refusal(request, token, worker_route)
            kind = _bearer_kind(token)
        else:
            token = request.cookies.get(SESSION_COOKIE)
            if not token:
                if worker_route:
                    return unauthorized(request, f"a worker token is required: {hint}")
                return unauthorized(request, f"sign-in required: {LOGIN_HINT}, or sign in on the web")
            kind = WEB
        refusal = self._channel_refusal(request, kind, worker_route)
        if refusal is not None:
            return refusal
        if kind == WEB and request.method in UNSAFE_METHODS:
            refusal = self._csrf_refusal(request, config, token)
            if refusal is not None:
                return refusal
        try:
            principal = await authenticate(request.app.state.engine, token, kind, config)
        except (
            psycopg.OperationalError,
            sa_exc.OperationalError,
            OSError,
        ) as exc:  # PoolTimeout is an OperationalError; bugs stay 500s
            log.warning("cannot check a credential: database unavailable", extra={"error": type(exc).__name__})
            return error_response(request, 503, "the hub database is unavailable; try again shortly")
        if principal is None:
            what = {MACHINE: "token", WEB: "web session", WORKER: "worker token", RUN: "run's token"}[kind]
            return unauthorized(request, f"the {what} is revoked, expired or unknown: {hint}", clear_cookie=kind == WEB)
        request.state.principal = principal
        if kind == RUN:  # after the database: a run's token that is no longer live gets 401 everywhere
            if principal.kg_project is not None:
                reason = kg_run_access.refusal(request.method, request.url.path, principal.kg_project)
            else:
                reason = run_access_rules.refusal(request.method, request.url.path, principal.run_reach)
            if reason is not None:
                log.warning("a run's token was refused", extra={"run_id": principal.run_id, "method": request.method})
                return error_response(request, 403, reason)
        return None

    @staticmethod
    def _ingest_refusal(request: Request, token: str, worker_route: bool) -> Response | None:
        # An ingest key writes signals and does nothing else: decided by the path and the method alone, before the
        # database. The route it may reach checks the key, its project and its rate (evo_agents.hub.signals).
        if worker_route or request.method != "POST" or not INGEST_ROUTE.fullmatch(request.url.path):
            return error_response(
                request, 403, "an ingest key (evi_...) only writes signals, with POST /v1/signals/{project}"
            )
        request.state.ingest_token = token
        return None

    @staticmethod
    def _protocol_refusal(request: Request) -> Response | None:
        given = request.headers.get(PROTOCOL_HEADER)
        if given is not None and given.strip() == PROTOCOL_VERSION:
            return None
        return error_response(
            request,
            426,
            f"this hub speaks version {PROTOCOL_VERSION} of the worker protocol and needs the header "
            f"{PROTOCOL_HEADER}: {PROTOCOL_VERSION}; upgrade evo-agents on the worker",
        )

    @staticmethod
    def _channel_refusal(request: Request, kind: str, worker_route: bool) -> Response | None:
        # Decided by the kind of credential alone, before the database: a worker token outside the worker's routes,
        # or anything else on them, is refused whether or not it is live.
        if worker_route and kind != WORKER:
            what = {WEB: "a web session", RUN: "a run's token"}.get(kind, "a machine token")
            return error_response(
                request, 403, f"{WORKER_PREFIX}* takes a worker token (evw_...), not {what}: {JOIN_HINT}"
            )
        if not worker_route and kind == WORKER:
            return error_response(
                request, 403, f"a worker token works only on {WORKER_PREFIX}*: sign in with `evo-agents hub login`"
            )
        return None

    @staticmethod
    def _csrf_refusal(request: Request, config, session_token: str) -> Response | None:
        # Checked before the database: a forged request costs a hash and an HMAC, nothing more.
        if not config.session_secret:
            return error_response(request, 503, "web sign-in is not configured on this hub (EVO_HUB_SESSION_SECRET)")
        given = request.headers.get(CSRF_HEADER, "")
        if not given or not same(given, csrf_token(config.session_secret, hash_token(session_token))):
            return error_response(
                request,
                403,
                f"a write made with the session cookie needs the {CSRF_HEADER} header from GET /v1/auth/web/csrf",
            )
        return None


def _bearer_kind(token: str) -> str:
    """The kind of a Bearer token by its prefix: WORKER, RUN, else MACHINE (which a malformed token fails as)."""
    for kind in (WORKER, RUN):
        if token.startswith(PREFIXES[kind]):
            return kind
    return MACHINE


def principal(request: Request) -> Principal:
    """The signed-in caller. ``Authenticate`` has refused the request already when there is none."""
    found = getattr(request.state, "principal", None)
    if found is None:  # a route outside the guarded prefix asked for a caller
        raise HTTPException(401, f"sign-in required: {LOGIN_HINT}", headers=WWW_AUTHENTICATE)
    return found


def admin(user: Annotated[Principal, Depends(principal)]) -> Principal:
    if not user.admin:
        raise HTTPException(403, "this needs a hub admin, a login listed in EVO_HUB_ADMINS")
    return user


CurrentUser = Annotated[Principal, Depends(principal)]
AdminUser = Annotated[Principal, Depends(admin)]
