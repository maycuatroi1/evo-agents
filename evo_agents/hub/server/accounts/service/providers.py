"""What the routes of provider accounts share, a member's (``provider_accounts``) and an agent's
(``agent_accounts``): the account as its owner sees it, the endpoint a write names, checked, the models it
lists, and the text of an audit row (the writes of its ``provider_accounts`` and ``accounts`` rows are in
``repository.provider_accounts``).

``endpoint`` takes the provider's entry in the catalog (``evo_agents.hub.model_catalog``): an entry the gateway serves
(``via`` gateway; the Claude subscription signs in instead), whose base URL and API format are the defaults a write may
change. A custom entry (custom-openai, custom-anthropic) has no base URL, so a write gives one. ``checked_url`` checks
it as ``public_endpoint`` says, its addresses included. ``listed_models`` takes the models a write typed, adds the
catalog's default model for ``models_from`` catalog, and those the endpoint lists for ``models_from`` endpoint, which
calls it with the key (``probe``); a write whose endpoint refuses the key gets 422 with the sanitized error.

The app's state may carry ``provider_transport`` (an httpx transport) and ``provider_resolver`` (an async function of
host and port to addresses): tests set them; the hub runs with neither, so it calls the network and its resolver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import AwareDatetime, SecretStr

from evo_agents.hub import model_catalog
from evo_agents.hub.accounts import account_state
from evo_agents.hub.credentials import MAX_SECRET_BYTES
from evo_agents.hub.provider_accounts import MAX_MODELS, model_refusal, unique_models
from evo_agents.hub.server.accounts.models.probe import Probe
from evo_agents.hub.server.accounts.models.provider_accounts import ModelsFrom, ProviderProbe
from evo_agents.hub.server.accounts.service.probe import probe
from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.public_endpoint import EndpointRefused, base_url, public_addresses
from evo_agents.hub.server.share_views import CredentialShare

# Reading


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
        raise Unprocessable(f"the catalog has no provider {provider}: see GET /v1/model-catalog, or use custom-openai")
    if entry["via"] != "gateway":
        raise Unprocessable(f"{entry['name']} signs in, with no API key: add it as a Claude account instead")
    url = url or entry["base_url"]
    if not url:
        raise Unprocessable(f"{entry['name']} has no base URL of its own: give base_url, the root of its API")
    try:
        return base_url(url), api_format or entry["api_format"]
    except EndpointRefused as exc:
        raise Unprocessable(str(exc)) from None


async def checked_url(request: Request, url: str) -> str:
    """``url`` when every address of its host is public now; 422 otherwise."""
    try:
        await public_addresses(url, getattr(request.app.state, "provider_resolver", None))
    except EndpointRefused as exc:
        raise Unprocessable(str(exc)) from None
    return url


def key_value(secret: SecretStr) -> str:
    value = secret.get_secret_value().strip()
    if not value or len(value.encode()) > MAX_SECRET_BYTES:
        raise Unprocessable(f"an API key is 1 to {MAX_SECRET_BYTES} bytes of UTF-8")
    if any(char in value for char in "\x00\r\n"):
        raise Unprocessable("an API key is one line: an HTTP header carries it")
    return value


def no_past_end(expires_at: AwareDatetime | None) -> None:
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise Unprocessable("expires_at is past already: an account's end is in the future")


async def call(request: Request, url: str, api_format: str, key: str, model: str | None) -> Probe:
    """``probe.probe`` with the app's transport and resolver, those of the tests or none."""
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
            raise Unprocessable(refusal)
    names = list(typed)
    if models_from == "catalog":
        default = (model_catalog.find(provider) or {}).get("default_model")
        if not default:
            raise Unprocessable(f"the catalog names no model of {provider}: type them, or fetch the endpoint's")
        names.append(default)
    elif models_from == "endpoint" and endpoint_of is not None:
        found = await call(request, *endpoint_of, typed[0] if typed else None)
        if not found.ok:
            raise Unprocessable(f"the endpoint refused to list its models: {found.error}")
        names += found.models
    names = unique_models(names)
    if len(names) > MAX_MODELS:
        raise Unprocessable(f"an account lists at most {MAX_MODELS} models")
    return names


def audit_text(secret_id: int, provider: str, url: str, fields: list[str] | None = None) -> str:
    """How an audit row names an account: by id, provider and host, never its name, key or the path of its URL."""
    named = f" fields={','.join(fields)}" if fields else ""
    return f"secret:{secret_id} provider={provider} host={urlsplit(url).hostname}{named}"
