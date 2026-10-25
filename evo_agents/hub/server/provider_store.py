"""What the routes of provider accounts share, a member's (``provider_accounts``) and an agent's
(``agent_provider_accounts``): the account as its owner sees it, the endpoint a write names, checked, the models it
lists, and the writes of its ``provider_accounts`` and ``accounts`` rows.

``endpoint`` takes the provider's entry in the catalog (``evo_agents.hub.model_catalog``): an entry the gateway serves
(``via`` gateway; the Claude subscription signs in instead), whose base URL and API format are the defaults a write may
change. A custom entry (custom-openai, custom-anthropic) has no base URL, so a write gives one. ``checked_url`` checks
it as ``public_endpoint`` says, its addresses included. ``listed_models`` takes the models a write typed, adds the
catalog's default model for ``models_from`` catalog, and those the endpoint lists for ``models_from`` endpoint, which
calls it with the key (``provider_probe``); a write whose endpoint refuses the key gets 422 with the sanitized error.

The app's state may carry ``provider_transport`` (an httpx transport) and ``provider_resolver`` (an async function of
host and port to addresses): tests set them; the hub runs with neither, so it calls the network and its resolver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field, SecretStr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import model_catalog, tables
from evo_agents.hub.accounts import ACCOUNT_STATES, MAX_PRIORITY, MAX_WEIGHT, account_state
from evo_agents.hub.credentials import MAX_SECRET_BYTES, PROVIDER_KIND
from evo_agents.hub.provider_accounts import API_FORMATS, MAX_MODELS, PROVIDER_ID, model_refusal, unique_models
from evo_agents.hub.server.provider_probe import Probe, probe
from evo_agents.hub.server.public_endpoint import EndpointRefused, base_url, public_addresses
from evo_agents.hub.server.secrets import bound_names
from evo_agents.hub.server.share_views import CredentialShare

ModelsFrom = Literal["endpoint", "catalog"]
NO_KEY_GIVEN = "give the API key: value, or from_secret, the name of one of your env secrets that holds it"


# Models


class ProviderAccount(BaseModel):
    """A provider account as its owner sees it: everything but its key."""

    name: str
    provider: str = Field(description="the id of its entry in the catalog (GET /v1/model-catalog)")
    provider_name: str = Field(description="the catalog's name of that provider")
    alias: str = Field(description="what the gateway reads before the slash of a model name: <alias>/<model>")
    base_url: str = Field(description="the https root of its API; every address it resolves to is public")
    api_format: Literal[API_FORMATS]
    models: list[str] = Field(description="the models it serves, by the endpoint's names")
    projects: list[str] = Field(description="the projects whose runs may take it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    weight: int = Field(description=f"0 to {MAX_WEIGHT}: its share in a weighted-round-robin pool; 0 leaves it")
    priority: int = Field(description=f"0 to {MAX_PRIORITY}: the lower, the sooner a fill-first pool takes it")
    paused_at: datetime | None = Field(description="when its owner paused it; null while it may be taken")
    cooldown_until: datetime | None = Field(description="until when it cools down after a usage limit")
    cooldown_reason: str | None = Field(description="why it cools down")
    last_leased_at: datetime | None = Field(description="when a run last took it")
    expires_at: datetime | None
    state: Literal[ACCOUNT_STATES] = Field(description="ready, cooling, paused or expired, now")
    shares: list[CredentialShare] = Field(default_factory=list, description="its shares not revoked, newest first")
    created_at: datetime
    updated_at: datetime


class ProviderAccountWritten(ProviderAccount):
    created: bool = Field(description="true when this write created it, false when it replaced one")


class ProviderProbe(BaseModel):
    """What the endpoint answered a call with the key: never the key, nor more than a line of its words."""

    ok: bool = Field(description="whether the endpoint took the key")
    status: int | None = Field(description="the HTTP status of its answer; null when none came")
    count: int = Field(description="how many models it listed")
    models: list[str] = Field(description="the models it listed, by its names")
    error: str | None = Field(description="why not, sanitized: no key, at most one short line of what it said")


class ProbeWrite(BaseModel):
    """An endpoint to try before an account is saved: the same fields as the write, the key included."""

    provider: str = Field(pattern=PROVIDER_ID.pattern, description="the id of an entry of the catalog")
    base_url: str | None = Field(None, description="the https root of its API; the catalog's when left out")
    api_format: Literal[API_FORMATS] | None = Field(None, description="the catalog's when left out")
    value: SecretStr | None = Field(None, min_length=1, max_length=MAX_SECRET_BYTES, description="the API key")
    from_secret: str | None = Field(None, description="or the name of one of your env secrets that holds it")
    model: str | None = Field(None, description="a model to ask, when the endpoint lists none")


class StoredProbe(BaseModel):
    model: str | None = Field(None, description="a model to ask, when the endpoint lists none; its first otherwise")


# Reading


def provider_rows(where, name: str | None = None):
    """The live provider accounts that ``where`` (a clause on secrets) keeps, with their endpoint, bindings and
    settings, by name; ``name`` alone, when given."""
    s, p, a, b = tables.secrets, tables.provider_accounts, tables.accounts, tables.secret_bindings
    statement = (
        select(
            s.c.id,
            s.c.name,
            p.c.provider,
            p.c.base_url,
            p.c.api_format,
            p.c.models,
            bound_names(tables.projects, b.c.project_id).label("projects"),
            bound_names(tables.workers, b.c.worker_id).label("workers"),
            func.coalesce(a.c.weight, 1).label("weight"),
            func.coalesce(a.c.priority, 0).label("priority"),
            a.c.paused_at,
            a.c.cooldown_until,
            a.c.cooldown_reason,
            a.c.last_leased_at,
            s.c.expires_at,
            s.c.created_at,
            s.c.updated_at,
            func.now().label("now"),
        )
        .join_from(s, p, p.c.secret_id == s.c.id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(where, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None))
        .order_by(s.c.name)
    )
    return statement if name is None else statement.where(s.c.name == name)


def view_of(row, shares: list[CredentialShare] | None = None) -> dict:
    """The fields of ``ProviderAccount`` from a row of ``provider_rows``."""
    values = dict(row._mapping)
    del values["id"]
    now = values.pop("now")
    entry = model_catalog.find(row.provider) or {}
    return {
        **values,
        "provider_name": entry.get("name", row.provider),
        "alias": entry.get("alias", row.provider),
        "state": account_state(row.paused_at, row.cooldown_until, row.expires_at, now),
        "shares": shares or [],
    }


def as_probe(found: Probe) -> ProviderProbe:
    return ProviderProbe(
        ok=found.ok, status=found.status, count=len(found.models), models=found.models, error=found.error
    )


# Checks


def endpoint(provider: str, url: str | None, api_format: str | None) -> tuple[str, str]:
    """(base URL, API format) of an account of ``provider`` given ``url`` and ``api_format``, the catalog's for what
    is left out; 422 for a provider the catalog has not, or does not reach through the gateway, and a bad URL."""
    entry = model_catalog.find(provider)
    if entry is None:
        raise HTTPException(
            422, f"the catalog has no provider {provider}: see GET /v1/model-catalog, or use custom-openai"
        )
    if entry["via"] != "gateway":
        raise HTTPException(422, f"{entry['name']} signs in, with no API key: add it as a Claude account instead")
    url = url or entry["base_url"]
    if not url:
        raise HTTPException(422, f"{entry['name']} has no base URL of its own: give base_url, the root of its API")
    try:
        return base_url(url), api_format or entry["api_format"]
    except EndpointRefused as exc:
        raise HTTPException(422, str(exc)) from None


async def checked_url(request: Request, url: str) -> str:
    """``url`` when every address of its host is public now; 422 otherwise."""
    try:
        await public_addresses(url, getattr(request.app.state, "provider_resolver", None))
    except EndpointRefused as exc:
        raise HTTPException(422, str(exc)) from None
    return url


def key_value(secret: SecretStr) -> str:
    value = secret.get_secret_value().strip()
    if not value or len(value.encode()) > MAX_SECRET_BYTES:
        raise HTTPException(422, f"an API key is 1 to {MAX_SECRET_BYTES} bytes of UTF-8")
    if any(char in value for char in "\x00\r\n"):
        raise HTTPException(422, "an API key is one line: an HTTP header carries it")
    return value


def no_past_end(expires_at: AwareDatetime | None) -> None:
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise HTTPException(422, "expires_at is past already: an account's end is in the future")


async def call(request: Request, url: str, api_format: str, key: str, model: str | None) -> Probe:
    """``provider_probe.probe`` with the app's transport and resolver, those of the tests or none."""
    state = request.app.state
    return await probe(
        url,
        api_format,
        key,
        model,
        transport=getattr(state, "provider_transport", None),
        resolver=getattr(state, "provider_resolver", None),
    )


async def listed_models(
    request: Request,
    typed: list[str],
    models_from: ModelsFrom | None,
    provider: str,
    endpoint_of: tuple[str, str, str] | None,
) -> list[str]:
    """The models of a write: those typed, then the catalog's or the endpoint's (``endpoint_of`` is URL, format and
    key); 422 for a bad name, too many, or an endpoint that refused the key."""
    for name in typed:
        if (refusal := model_refusal(name)) is not None:
            raise HTTPException(422, refusal)
    names = list(typed)
    if models_from == "catalog":
        default = (model_catalog.find(provider) or {}).get("default_model")
        if not default:
            raise HTTPException(422, f"the catalog names no model of {provider}: type them, or fetch the endpoint's")
        names.append(default)
    elif models_from == "endpoint" and endpoint_of is not None:
        found = await call(request, *endpoint_of, typed[0] if typed else None)
        if not found.ok:
            raise HTTPException(422, f"the endpoint refused to list its models: {found.error}")
        names += found.models
    names = unique_models(names)
    if len(names) > MAX_MODELS:
        raise HTTPException(422, f"an account lists at most {MAX_MODELS} models")
    return names


# Writing


async def store_endpoint(conn: AsyncConnection, secret_id: int, **values) -> None:
    """The account's ``provider_accounts`` row, made or changed: provider, base_url, api_format and models given."""
    p = tables.provider_accounts
    made = pg_insert(p).values(secret_id=secret_id, **values)
    await conn.execute(
        made.on_conflict_do_update(index_elements=[p.c.secret_id], set_={**values, "updated_at": func.now()})
    )


async def store_settings(conn: AsyncConnection, secret_id: int, weight=None, priority=None, paused=None) -> None:
    """Weight, priority and pause of the account, in its ``accounts`` row (made when there is none)."""
    a = tables.accounts
    values = {key: given for key, given in (("weight", weight), ("priority", priority)) if given is not None}
    if paused is not None:
        values["paused_at"] = func.now() if paused else None
    if not values:
        return
    kept = {**values}
    if paused:
        kept["paused_at"] = func.coalesce(a.c.paused_at, func.now())
    made = pg_insert(a).values(secret_id=secret_id, **values)
    await conn.execute(
        made.on_conflict_do_update(index_elements=[a.c.secret_id], set_={**kept, "updated_at": func.now()})
    )


def audit_text(secret_id: int, provider: str, url: str, fields: list[str] | None = None) -> str:
    """How an audit row names an account: by id, provider and host, never its name, key or the path of its URL."""
    named = f" fields={','.join(fields)}" if fields else ""
    return f"secret:{secret_id} provider={provider} host={urlsplit(url).hostname}{named}"
