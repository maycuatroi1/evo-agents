"""Runs on a provider account's model, through the model gateway behind the hub (plan model-providers).

A provider account's models are named as the gateway reads them, ``<alias>/<model>``: the alias is the catalog's for
the account's provider (``model_catalog``), or ``evo-<secret id>`` for an endpoint a member named (custom-openai,
custom-anthropic), the prefix of its node at the gateway (``server.gateway_sync``). A run whose model has such a prefix
records it in runs.gateway_provider; only a daemon of GATEWAY_RUN_AGENT or later claims it, as an older one would run
it on its own sign-in. The worker's lease of such a run (kind and provider ``gateway``) carries the hub's gateway
route, a token of the run, the model and the API format its runtime speaks (RUNTIME_FORMATS); never a provider's key
nor the gateway's. A pure module: the server, the CLI and the worker read it.
"""

from __future__ import annotations

import re

from evo_agents.hub import model_catalog

GATEWAY = "gateway"  # credential_leases.provider and the kind of a gateway lease
GATEWAY_RUN_AGENT = (0, 45, 0)  # the first daemon release that runs a run through the gateway route
RUNTIME_FORMATS = {"claude-code": "anthropic", "opencode": "openai", "codex": "openai-responses"}
GATEWAY_RUNTIMES = tuple(RUNTIME_FORMATS)
CUSTOM_PROVIDERS = ("custom-openai", "custom-anthropic")  # a node of its own at the gateway, prefixed evo-<id>
CUSTOM_PREFIX = re.compile(r"evo-([1-9][0-9]{0,17})")
PICK_PREFIX = "gateway:"  # run_accounts.env_var of a provider account picked by hand: gateway:<prefix>
ROUTE = "/v1"  # where the hub serves the gateway's client endpoints to runs


def takes_gateway_runs(agent_version: str | None) -> bool:
    """Whether a daemon of ``agent_version`` runs a gateway run: GATEWAY_RUN_AGENT or later; a version that cannot be
    read does not."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", (agent_version or "").strip())
    if match is None:
        return False
    return tuple(int(part or 0) for part in match.groups()) >= GATEWAY_RUN_AGENT


def prefix_of(provider: str, secret_id: int) -> str | None:
    """The prefix of the models of a provider account, as the gateway reads it; None for a provider the gateway does
    not serve (the Claude subscription signs in on the worker)."""
    if provider in CUSTOM_PROVIDERS:
        return f"evo-{secret_id}"
    entry = model_catalog.find(provider)
    if entry is None or entry.get("via") != GATEWAY:
        return None
    return entry["alias"]


def model_name(prefix: str, model: str) -> str:
    return f"{prefix}/{model}"


def split(model: str | None) -> tuple[str, str] | None:
    """(prefix, the provider's model) of ``model`` when its prefix is one the gateway serves: a catalog alias of a
    gateway provider, or evo-<id>; None for any other model."""
    if not model or "/" not in model:
        return None
    prefix, _, rest = model.partition("/")
    if not rest:
        return None
    if CUSTOM_PREFIX.fullmatch(prefix) or prefix in _aliases():
        return prefix, rest
    return None


def providers_of(prefix: str) -> tuple[str, ...]:
    """The catalog ids whose models ``prefix`` names: the provider of that alias, or both custom kinds for evo-<id>."""
    if CUSTOM_PREFIX.fullmatch(prefix):
        return CUSTOM_PROVIDERS
    return tuple(entry["id"] for entry in model_catalog.providers() if entry["alias"] == prefix)


def custom_id(prefix: str) -> int | None:
    """The secret id of an endpoint's prefix evo-<id>; None for a catalog alias."""
    match = CUSTOM_PREFIX.fullmatch(prefix)
    return int(match.group(1)) if match else None


def _aliases() -> frozenset[str]:
    return frozenset(
        entry["alias"]
        for entry in model_catalog.providers()
        if entry.get("via") == GATEWAY and entry["id"] not in CUSTOM_PROVIDERS
    )
