"""Add or renew a Claude account from the web page, with no terminal (``docs/credentials.md``, section "Signing in on
the web").

POST /v1/accounts/claude-sign-in starts a sign-in: the hub draws a PKCE verifier and a state, seals them with the time
for the caller alone (associated data ``claude-sign-in:{user id}``), and answers the page of claude.ai to open and the
sealed flow, which the page keeps and sends back. Once the member approves, claude.ai shows a code (the redirect Claude
Code uses when it cannot listen on localhost); the member pastes it, and POST /v1/accounts/claude-sign-in/finish opens
the flow (the caller's own, younger than FLOW_TTL), checks the name and the bindings first, so a refusal spends no
code, trades the code for the token `claude setup-token` makes (inference only, a year) and writes it as PUT
/v1/secrets/{name} does: a new account, or a new token for one of the caller's Claude accounts, whose bindings stay
unless the body names others. The verifier, the code and the token are in no response, log line or row in the clear.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from evo_agents.hub import claude_oauth
from evo_agents.hub.credentials import MAX_SECRETS_PER_OWNER, SECRET_NAME
from evo_agents.hub.server import route_autonomy
from evo_agents.hub.server.account_oauth import OAUTH_ENV_VAR
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models.placement import PoolName
from evo_agents.hub.server.sealing import Sealer, Unsealable
from evo_agents.hub.server.secrets import (
    MAX_BINDINGS,
    PROVIDER_HOLDS,
    Secret,
    SecretWrite,
    SecretWritten,
    binding_refusal,
    live_secrets,
    own_pools,
    own_workers,
    owned_secrets,
    provider_holds,
    put_secret,
    sealer_of,
    unique_names,
    writable_projects,
)
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.worker_models import WORKER_NAME

log = logging.getLogger(__name__)

FLOW_TTL = timedelta(minutes=15)
RESTART = "click Sign in with Claude again"
NOT_YOURS = f"this sign-in is not yours, or was changed on the way: {RESTART}"
TOO_OLD = f"this sign-in started over {int(FLOW_TTL.total_seconds() // 60)} minutes ago: {RESTART}"
NO_PROJECT = "pick at least one project: an account bound to none is leased to no run"
OTHER_SECRET = "{name} is a secret of yours that is no Claude account: pick another name"

router = APIRouter(
    prefix="/v1/accounts/claude-sign-in",
    tags=["accounts"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}},
)


class SignInStarted(BaseModel):
    authorize_url: str = Field(description="the page of claude.ai to approve the sign-in on; it then shows a code")
    flow: str = Field(description="the sign-in, sealed for the caller: send it back with the code")
    expires_at: datetime = Field(description="after this the flow is refused and the sign-in starts again")


class SignInFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow: str = Field(min_length=1, max_length=4096, description="what POST /v1/accounts/claude-sign-in gave")
    code: SecretStr = Field(
        min_length=1,
        max_length=4096,
        description="what claude.ai showed after the approval: code#state, the bare code or the address it went to",
    )
    name: Annotated[str, Field(pattern=f"^{SECRET_NAME.pattern}$")] = Field(
        description="the account: a new one, or one of the caller's Claude accounts to give the new token"
    )
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] | None = Field(
        None,
        max_length=MAX_BINDINGS,
        description="the projects whose runs may take it, the writer role on each; left out to keep an account's",
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] | None = Field(
        None, max_length=MAX_BINDINGS, description="the caller's workers it is for; left out to keep an account's"
    )
    pools: list[PoolName] | None = Field(
        None, max_length=MAX_BINDINGS, description="the caller's worker pools it is for; never with workers"
    )


class ClaudeSignedIn(SecretWritten):
    email: str | None = Field(description="the Claude account that approved, when claude.ai named it")


def flow_aad(user_id: int) -> bytes:
    """The associated data of a sealed sign-in: it opens only for the member who started it."""
    return f"claude-sign-in:{user_id}".encode()


def seal_flow(sealer: Sealer, user_id: int, pkce: claude_oauth.Pkce, now: datetime) -> str:
    """The verifier, the state and the time, sealed for ``user_id``, as one URL-safe word."""
    document = json.dumps({"verifier": pkce.verifier, "state": pkce.state, "at": int(now.timestamp())})
    stored = sealer.seal_stored(document, flow_aad(user_id))
    return base64.urlsafe_b64encode(json.dumps(stored).encode()).decode().rstrip("=")


def open_flow(sealer: Sealer, user_id: int, flow: str, now: datetime) -> claude_oauth.Pkce:
    """The PKCE ``flow`` holds; 400 when it is another member's, altered, or older than FLOW_TTL."""
    try:
        stored = json.loads(base64.urlsafe_b64decode(flow + "=" * (-len(flow) % 4)))
        document = json.loads(sealer.open_stored(stored, flow_aad(user_id)))
        verifier, state, at = document["verifier"], document["state"], document["at"]
        started = datetime.fromtimestamp(at, UTC)
    except (ValueError, TypeError, KeyError, binascii.Error, Unsealable, OverflowError, OSError):
        raise HTTPException(400, NOT_YOURS) from None
    if not isinstance(verifier, str) or not isinstance(state, str):
        raise HTTPException(400, NOT_YOURS)
    if now - started > FLOW_TTL or started > now + timedelta(minutes=1):
        raise HTTPException(400, TOO_OLD)
    return claude_oauth.pkce_of(verifier, state)


@router.post("", response_model=SignInStarted, responses={503: {"model": ErrorBody}})
async def start_sign_in(request: Request, user: CurrentUser, response: Response) -> SignInStarted:
    """Start a Claude sign-in: the page to open, and the sealed flow to send back with the code it shows."""
    sealer = sealer_of(request)
    now = datetime.now(UTC)
    pkce = claude_oauth.new_pkce()
    response.headers.update(NO_STORE)
    return SignInStarted(
        authorize_url=claude_oauth.setup_token_url(pkce),
        flow=seal_flow(sealer, user.user_id, pkce, now),
        expires_at=now + FLOW_TTL,
    )


def _account_of(found: list[Secret], name: str) -> Secret | None:
    """The caller's Claude account ``name``; 409 when the name is a secret of another kind."""
    if not found:
        return None
    (secret,) = found
    if secret.kind != "env" or secret.env_var != OAUTH_ENV_VAR:
        raise HTTPException(409, OTHER_SECRET.format(name=name))
    return secret


@router.post(
    "/finish",
    response_model=ClaudeSignedIn,
    responses={
        400: {"model": ErrorBody},
        403: {"model": ErrorBody},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
        502: {"model": ErrorBody},
        503: {"model": ErrorBody},
    },
)
async def finish_sign_in(request: Request, body: SignInFinish, user: CurrentUser, response: Response) -> ClaudeSignedIn:
    """Trade the pasted code for the token of a year and keep it as the Claude account ``name``."""
    sealer = sealer_of(request)
    pkce = open_flow(sealer, user.user_id, body.flow, datetime.now(UTC))
    try:
        code = claude_oauth.parse_code(body.code.get_secret_value(), pkce.state, RESTART)
    except claude_oauth.OAuthError as exc:
        raise HTTPException(422, str(exc)) from None
    engine = request.app.state.engine
    async with engine.connect() as conn:
        existing = _account_of(await owned_secrets(conn, user.user_id, body.name), body.name)
    kept = (existing.projects, existing.workers, existing.pools) if existing else ([], [], [])
    projects = unique_names(body.projects if body.projects is not None else list(kept[0]))
    if body.workers is None and body.pools is None:  # the account's binding stays
        workers, pools = list(kept[1]), list(kept[2])
    else:
        workers, pools = unique_names(body.workers or []), unique_names(body.pools or [])
    if not projects:
        raise HTTPException(422, NO_PROJECT)
    refusal = binding_refusal("env", workers, pools)
    if refusal is not None:
        raise HTTPException(422, refusal)
    # The checks of the write, before the code is spent: a code buys one token, once.
    async with engine.connect() as conn:
        await writable_projects(conn, user, projects, "env")
        await own_workers(conn, user, workers)
        await own_pools(conn, user, pools)
        if await provider_holds(conn, user.user_id, body.name):
            raise HTTPException(409, PROVIDER_HOLDS.format(name=body.name))
        if existing is None and await live_secrets(conn, user.user_id, body.name) >= MAX_SECRETS_PER_OWNER:
            raise HTTPException(
                409, f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another"
            )
    try:
        got = await asyncio.to_thread(claude_oauth.exchange_setup_token, code, pkce)
    except claude_oauth.OAuthError as exc:
        log.warning(
            "claude sign-in on the web refused", extra={"secret": body.name, "login": user.login, "why": str(exc)}
        )
        raise HTTPException(502, f"Claude did not give a token ({exc}): {RESTART}") from None
    # A day before the token would fail, as `hub account login` stops leasing it.
    expires_at = (
        got.expires_at - timedelta(days=1) if got.expires_at - datetime.now(UTC) > timedelta(days=2) else got.expires_at
    )
    write = SecretWrite(
        kind="env",
        env_var=OAUTH_ENV_VAR,
        projects=projects,
        workers=workers,
        pools=pools,
        expires_at=expires_at,
        value=SecretStr(got.token),
    )
    written = await put_secret(request, body.name, write, user, response)
    log.info(
        "claude account signed in on the web",
        extra={"secret": body.name, "login": user.login, "replaced": not written.created},
    )
    response.headers.update(NO_STORE)
    return ClaudeSignedIn(**written.model_dump(), email=got.email)
