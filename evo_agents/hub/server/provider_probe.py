"""Test connection and Fetch models of a provider account: the hub calls the endpoint with the key and says what it
answered, never the key.

``probe`` checks the base URL again (``public_endpoint``), resolves its host and calls the first address it checked,
with the host name in the Host header and in TLS (SNI and the certificate's name), so a resolver that answers
otherwise on the second ask cannot turn the call to a private address. It asks the models path of the format first
(GET {base_url}/models: a Bearer key for openai and openai-responses, x-api-key with anthropic-version for anthropic,
x-goog-api-key for gemini). An endpoint without that path (404 or 405) gets the smallest request of its format with
``model``: a completion of one token. It follows no redirect, waits at most TIMEOUT_S seconds and reads at most
MAX_BODY_BYTES of the answer.

The result says whether the endpoint took the key, the models it listed, and an error made for a member to read: the
status and at most MAX_ERROR_CHARS of what the endpoint said, on one line, with the key and the bearer of any
Authorization blotted out. No exception, log line or result holds the key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from evo_agents.hub.provider_accounts import MAX_MODELS, MODEL_NAME
from evo_agents.hub.server.public_endpoint import EndpointRefused, Resolver, base_url, public_addresses

TIMEOUT_S = 10.0
MAX_BODY_BYTES = 1 << 20  # what the hub reads of one answer
MAX_ERROR_CHARS = 200
ANTHROPIC_VERSION = "2023-06-01"
NO_MODELS_PATH = (404, 405)  # an endpoint without GET /models
BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"']+")
KEYISH = re.compile(r"(?i)\b(sk|key|api[_-]?key|token)[-_=:\s\"']*[A-Za-z0-9._-]{12,}")


@dataclass
class Probe:
    ok: bool
    models: list[str] = field(default_factory=list)
    status: int | None = None  # the endpoint's HTTP status; None when no answer came
    error: str | None = None


def sanitized(text: str, key: str) -> str:
    """``text`` on one line, at most MAX_ERROR_CHARS, without ``key``, a bearer token or anything shaped as a key."""
    if key:
        text = text.replace(key, "[key]")
    text = BEARER.sub(r"\1[key]", text)
    text = KEYISH.sub("[key]", text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 3] + "..."


def _headers(api_format: str, key: str, host: str) -> dict[str, str]:
    headers = {"Host": host, "Accept": "application/json", "User-Agent": "evo-agents-hub"}
    if api_format == "anthropic":
        return headers | {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if api_format == "gemini":
        return headers | {"x-goog-api-key": key}
    return headers | {"Authorization": f"Bearer {key}"}


def _smallest(api_format: str, model: str) -> tuple[str, dict]:
    """(path, body) of the smallest request of ``api_format`` asking ``model`` for one token."""
    if api_format == "anthropic":
        return "/messages", {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    if api_format == "openai-responses":
        return "/responses", {"model": model, "input": "hi", "max_output_tokens": 16}
    if api_format == "gemini":
        return f"/models/{model}:generateContent", {"contents": [{"parts": [{"text": "hi"}]}]}
    return "/chat/completions", {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}


def listed_models(api_format: str, body: object) -> list[str]:
    """The model names an answer of GET /models lists: data[].id, or models[].name without models/ for gemini."""
    if not isinstance(body, dict):
        return []
    items = body.get("models") if api_format == "gemini" else body.get("data")
    if not isinstance(items, list):
        items = body.get("data") or body.get("models") or []
    names = []
    for item in items if isinstance(items, list) else []:
        name = item.get("id") or item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str):
            name = name.removeprefix("models/")
            if MODEL_NAME.fullmatch(name):
                names.append(name)
    return list(dict.fromkeys(names))[:MAX_MODELS]


def _pinned(url: str, address: str) -> str:
    """``url`` with its host replaced by ``address``, which the hub checked; the port and path kept."""
    parts = urlsplit(url)
    netloc = f"[{address}]" if ":" in address else address
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


async def _read(response: httpx.Response) -> bytes:
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) >= MAX_BODY_BYTES:
            break
    return body[:MAX_BODY_BYTES]


async def _call(client: httpx.AsyncClient, method: str, url: str, headers: dict, payload, host: str):
    request = client.build_request(method, url, headers=headers, json=payload, extensions={"sni_hostname": host})
    response = await client.send(request, stream=True)
    try:
        return response.status_code, await _read(response)
    finally:
        await response.aclose()


def _failed(status: int, body: bytes, key: str) -> Probe:
    if 300 <= status < 400:
        return Probe(False, status=status, error=f"HTTP {status}: the endpoint answered with a redirect, not followed")
    said = sanitized(body.decode("utf-8", "replace"), key)
    return Probe(False, status=status, error=f"HTTP {status}" + (f": {said}" if said else ""))


async def probe(
    url: str,
    api_format: str,
    key: str,
    model: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> Probe:
    """Call the endpoint at ``url`` with ``key`` as described above."""
    try:
        url = base_url(url)
        address = (await public_addresses(url, resolver))[0]
    except EndpointRefused as exc:
        return Probe(False, error=str(exc))
    host = urlsplit(url).hostname or ""
    netloc_host = urlsplit(url).netloc
    headers = _headers(api_format, key, netloc_host)
    pinned = _pinned(url, address)
    timeout = httpx.Timeout(TIMEOUT_S)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False) as client:
            status, body = await _call(client, "GET", pinned + "/models", headers, None, host)
            if status in NO_MODELS_PATH and model:
                path, payload = _smallest(api_format, model)
                status, body = await _call(client, "POST", pinned + path, headers, payload, host)
                return Probe(True, [model], status) if 200 <= status < 300 else _failed(status, body, key)
    except httpx.TimeoutException:
        return Probe(False, error=f"the endpoint did not answer within {TIMEOUT_S:g} seconds")
    except httpx.HTTPError as exc:
        return Probe(False, error=f"the endpoint could not be reached: {sanitized(type(exc).__name__, key)}")
    if not 200 <= status < 300:
        if status in NO_MODELS_PATH:
            return Probe(
                False, status=status, error=f"HTTP {status}: the endpoint lists no models; name a model to test"
            )
        return _failed(status, body, key)
    try:
        found = json.loads(body)
    except ValueError:
        return Probe(False, status=status, error="the endpoint's list of models is not JSON")
    return Probe(True, listed_models(api_format, found), status)
