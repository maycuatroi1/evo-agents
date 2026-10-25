"""Provider accounts as both ends model them: an API key of a model provider that a member, or an agent, keeps on the
hub with the endpoint it calls and the models it serves (``evo_agents.hub.server.provider_accounts``). Standard
library only, so the CLI shares it with the api.

A provider account is a secret of kind ``provider`` (``credentials.PROVIDER_KIND``) and a row of
``provider_accounts``: ``provider`` is the id of an entry of the catalog (``evo_agents.hub.model_catalog``),
custom-openai or custom-anthropic for an endpoint the member names alone; ``base_url`` is the https root of its API, the catalog's by default;
``api_format`` one of API_FORMATS, which says how the gateway, and the hub's test of it, talk to that root; ``models``
the names of the models it serves, as the endpoint listed them, as the catalog has them, or as the member typed them.
"""

from __future__ import annotations

import re

API_FORMATS = ("openai", "anthropic", "openai-responses", "gemini")  # as the catalog's api_format
PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # a catalog id
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")  # what an endpoint names a model, one line
MAX_MODELS = 500  # the models one account lists
MAX_BASE_URL_CHARS = 2000


def model_refusal(name: str) -> str | None:
    """Why ``name`` cannot be a model of an account, or None."""
    if MODEL_NAME.fullmatch(name):
        return None
    return (
        f"{name[:60]!r} is not a model name: 1 to 200 letters, digits and . _ : / @ + -, starting with a letter or a "
        "digit"
    )


def unique_models(names: list[str]) -> list[str]:
    """``names`` without repeats, in their order."""
    return list(dict.fromkeys(name.strip() for name in names if name.strip()))
