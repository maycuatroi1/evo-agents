"""The api's client to the model gateway: OmniRoute, pinned by digest (``GATEWAY_IMAGE``), a service of
deploy/hub/docker-compose.yml that only the api reaches, on a network of its own (docs/hub.md, section Model gateway).

One root secret, EVO_HUB_GATEWAY_SECRET, configures both ends. The gateway's entrypoint derives from it the variables
OmniRoute needs (``DERIVED``: its JWT and API key secrets, the key of its storage, the password it starts with), each
the HMAC-SHA256 of ``CONTEXT`` and a label under the root secret, in hex; the api derives the same password and signs in
with it (``POST /api/auth/login``), so no one copies a credential by hand and none is stored: the session cookie lives
in the api's memory, and a 401 signs in again. Every call has a short timeout. ``GatewayError`` names the call and the
status, never a body, which may echo a key.

The typed objects are what the hub reads of the gateway's own: ``Connection`` (a provider account's key, of a catalog
provider or of a ``Node``, an endpoint a member named), ``GatewayKey`` (an API key of the gateway, restricted to some
connections, with an end, never logging a request: ``create_key`` always sets noLog) and ``KeyUsage`` (the requests,
tokens and cost the gateway counted for one key). A provider's key goes to the gateway and nowhere else; the value of a
gateway key comes back once, from ``create_key``, as a ``SecretStr``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from evo_agents.hub.config import HubConfig
from evo_agents.hub.log import register_secret
from evo_agents.hub.model_catalog import GATEWAY_VERSION

log = logging.getLogger(__name__)

GATEWAY_DIGEST = "sha256:b08a12e876baa8a5ccadd1ebbe346fe7267dba3b1c9f4fd6bb02070fae41ab6c"
GATEWAY_IMAGE = f"diegosouzapw/omniroute:{GATEWAY_VERSION}@{GATEWAY_DIGEST}"
CONTEXT = "evo-hub-gateway/"  # what each derived value is the HMAC of, before its label
DERIVED = {  # the gateway's variable, and the label its value is derived under
    "JWT_SECRET": "jwt",
    "API_KEY_SECRET": "api-key",
    "STORAGE_ENCRYPTION_KEY": "storage",
    "INITIAL_PASSWORD": "password",
}
TIMEOUT = 10.0  # seconds for one call, connecting included
RELAY_TIMEOUT = httpx.Timeout(600.0, connect=TIMEOUT)  # a model's answer, streamed or not, may take minutes
HEALTH_TIMEOUT = 3.0
SESSION_COOKIE = "auth_token"  # OmniRoute's dashboard session
OFF = "the model gateway is not configured: set EVO_HUB_GATEWAY_SECRET (docs/hub.md, section Model gateway)"


def zulu(moment: datetime) -> str:
    """``moment`` in UTC as the gateway's expiresAt takes it, ending in Z: OmniRoute's zod datetime() refuses an
    offset such as +00:00 with a 400."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def derive(secret: str, label: str) -> str:
    """The value the gateway's entrypoint derives from the root ``secret`` under ``label``, in hex."""
    return hmac.new(secret.encode(), (CONTEXT + label).encode(), hashlib.sha256).hexdigest()


class GatewayOff(Exception):
    """The hub runs without a gateway: EVO_HUB_GATEWAY_SECRET is not set."""

    def __init__(self) -> None:
        super().__init__(OFF)


class GatewayError(Exception):
    """A call to the gateway failed; the message names the call and the status, never what the gateway answered."""

    def __init__(self, what: str, status: int | None = None) -> None:
        super().__init__(f"the model gateway failed to {what}" + (f" (HTTP {status})" if status else ""))
        self.status = status


@dataclass(frozen=True)
class Connection:
    id: str
    provider: str  # a provider of the gateway's registry, or the id of a Node
    name: str
    active: bool
    # what the gateway last saw of it: testStatus (active, error, unavailable, expired...), its error's code and type,
    # and until when it rests after a rate limit (ISO 8601); never lastError, whose text may echo what the provider said
    test_status: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    limited_until: str | None = None
    base_url: str | None = None  # its providerSpecificData.baseUrl, which overrides its registry's URL when set


@dataclass(frozen=True)
class Node:
    id: str
    kind: str  # openai-compatible or anthropic-compatible
    name: str
    prefix: str  # what the gateway reads before the slash of a model name
    base_url: str
    api_type: str  # chat or responses


@dataclass(frozen=True)
class GatewayKey:
    id: str
    name: str
    allowed_connections: tuple[str, ...]
    expires_at: str | None
    no_log: bool
    revoked: bool = False


@dataclass(frozen=True)
class IssuedKey(GatewayKey):
    value: SecretStr = field(default=SecretStr(""), repr=False)


@dataclass(frozen=True)
class KeyUsage:
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def _text(value) -> str | None:
    return None if value is None or value == "" else str(value)


def _connection(data: dict) -> Connection:
    return Connection(
        str(data["id"]),
        str(data["provider"]),
        str(data.get("name") or ""),
        bool(data.get("isActive")),
        _text(data.get("testStatus")),
        _text(data.get("errorCode")),
        _text(data.get("lastErrorType")),
        _text(data.get("rateLimitedUntil")),
        _text((data.get("providerSpecificData") or {}).get("baseUrl")),
    )


def _node(data: dict) -> Node:
    return Node(
        str(data["id"]),
        str(data.get("type") or ""),
        str(data.get("name") or ""),
        str(data.get("prefix") or ""),
        str(data.get("baseUrl") or ""),
        str(data.get("apiType") or ""),
    )


def _key(data: dict) -> GatewayKey:
    return GatewayKey(
        str(data["id"]),
        str(data.get("name") or ""),
        tuple(str(found) for found in data.get("allowedConnections") or ()),
        data.get("expiresAt"),
        bool(data.get("noLog")),
        bool(data.get("revokedAt")),
    )


class GatewayClient:
    """The gateway at ``url``, signed in with the password derived from ``secret``. ``transport`` replaces the
    network in tests."""

    def __init__(self, url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url.rstrip("/")
        self._password = derive(secret, DERIVED["INITIAL_PASSWORD"])
        register_secret(self._password)
        self._cookie: str | None = None
        self._http = httpx.AsyncClient(base_url=self.url, timeout=TIMEOUT, transport=transport, follow_redirects=False)

    @classmethod
    def from_config(cls, config: HubConfig, transport: httpx.AsyncBaseTransport | None = None) -> GatewayClient | None:
        """The client of the configured gateway, or None without EVO_HUB_GATEWAY_SECRET."""
        if not config.gateway_secret or not config.gateway_url:
            return None
        return cls(config.gateway_url, config.gateway_secret, transport)

    def __repr__(self) -> str:
        return f"GatewayClient(url={self.url!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def healthy(self) -> bool:
        """Whether the gateway answers its health check within HEALTH_TIMEOUT."""
        try:
            response = await self._http.get("/api/health", timeout=HEALTH_TIMEOUT)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def _sign_in(self) -> str:
        try:
            response = await self._http.post("/api/auth/login", json={"password": self._password})
        except httpx.HTTPError as exc:
            raise GatewayError(f"sign in ({type(exc).__name__})") from None
        cookie = response.cookies.get(SESSION_COOKIE)
        if response.status_code != 200 or not cookie:
            raise GatewayError(
                "sign in: was its volume made with another EVO_HUB_GATEWAY_SECRET?", response.status_code
            )
        register_secret(cookie)
        self._http.cookies.clear()  # the session goes in the header of each call, not in a jar
        self._cookie = cookie
        return cookie

    async def _call(self, method: str, path: str, what: str, body: dict | None = None, params=None) -> dict:
        """The JSON the gateway answers, signed in first when the session is missing or refused once."""
        for attempt in (1, 2):
            cookie = self._cookie or await self._sign_in()
            try:
                response = await self._http.request(
                    method, path, json=body, params=params, headers={"cookie": f"{SESSION_COOKIE}={cookie}"}
                )
            except httpx.HTTPError as exc:
                raise GatewayError(f"{what} ({type(exc).__name__})") from None
            if response.status_code == 401 and attempt == 1:
                self._cookie = None
                continue
            if response.status_code >= 300:
                raise GatewayError(what, response.status_code)
            try:
                return response.json()
            except ValueError:
                raise GatewayError(f"{what}: the answer is not JSON") from None
        raise GatewayError(what, 401)

    # Connections and the nodes of endpoints members named

    async def connections(self) -> list[Connection]:
        found = await self._call("GET", "/api/providers", "list its connections")
        return [_connection(item) for item in found.get("connections") or []]

    async def create_connection(self, provider: str, name: str, key: str, base_url: str | None = None) -> Connection:
        """A connection of ``provider`` with ``key``, at ``base_url`` when given; the gateway makes it inactive."""
        body = {"provider": provider, "name": name, "apiKey": key}
        if base_url:
            body["providerSpecificData"] = {"baseUrl": base_url}
        found = await self._call("POST", "/api/providers", "create a connection", body)
        return _connection(found["connection"])

    async def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        key: str | None = None,
        base_url: str | None = None,
        active: bool | None = None,
    ) -> Connection:
        body: dict = {"name": name, "apiKey": key, "isActive": active}
        body = {name: value for name, value in body.items() if value is not None}
        if base_url:
            body["providerSpecificData"] = {"baseUrl": base_url}
        found = await self._call("PUT", f"/api/providers/{connection_id}", "update a connection", body)
        return _connection(found["connection"])

    async def delete_connection(self, connection_id: str) -> None:
        await self._call("DELETE", f"/api/providers/{connection_id}", "delete a connection")

    async def nodes(self) -> list[Node]:
        found = await self._call("GET", "/api/provider-nodes", "list its provider nodes")
        return [_node(item) for item in found.get("nodes") or []]

    async def create_node(self, name: str, prefix: str, kind: str, api_type: str, base_url: str) -> Node:
        body = {"name": name, "prefix": prefix, "type": kind, "apiType": api_type, "baseUrl": base_url}
        found = await self._call("POST", "/api/provider-nodes", "create a provider node", body)
        return _node(found["node"])

    async def delete_node(self, node_id: str) -> None:
        """The node and, with it, its connections."""
        await self._call("DELETE", f"/api/provider-nodes/{node_id}", "delete a provider node")

    # API keys of the gateway and their usage

    async def keys(self) -> list[GatewayKey]:
        found = await self._call("GET", "/api/keys", "list its API keys")
        return [_key(item) for item in found.get("keys") or []]

    async def create_key(self, name: str, connections: list[str], expires_at: datetime | None = None) -> IssuedKey:
        """A key that reaches only ``connections``, until ``expires_at`` when given; no request made with it is
        logged."""
        ends = None if expires_at is None else zulu(expires_at)
        body = {"name": name, "allowedConnections": list(connections), "noLog": True, "expiresAt": ends}
        found = await self._call("POST", "/api/keys", "create an API key", body)
        register_secret(found.get("key"))
        made = _key(found)
        return IssuedKey(**vars(made), value=SecretStr(str(found.get("key") or "")))

    async def revoke_key(self, key_id: str) -> None:
        await self._call("DELETE", f"/api/keys/{key_id}", "revoke an API key")

    async def key_usage(self, key_id: str) -> KeyUsage:
        params = {"apiKeyIds": key_id, "range": "all"}
        found = await self._call("GET", "/api/usage/analytics", "read the usage of a key", params=params)
        summary = found.get("summary") or {}
        return KeyUsage(
            int(summary.get("totalRequests") or 0),
            int(summary.get("promptTokens") or 0),
            int(summary.get("completionTokens") or 0),
            float(summary.get("totalCost") or 0.0),
        )

    # A run's requests to a model, relayed by the hub's gateway route (``gateway_proxy``)

    async def relay(self, method: str, path: str, key: str, headers: dict[str, str], content: bytes) -> httpx.Response:
        """Send a run's request to the gateway's client endpoint ``path`` with the gateway's ``key`` and return its
        answer unread, to stream: the caller closes it. RELAY_TIMEOUT between two chunks."""
        sent = {**headers, "authorization": f"Bearer {key}"}
        request = self._http.build_request(method, path, headers=sent, content=content, timeout=RELAY_TIMEOUT)
        try:
            return await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise GatewayError(f"relay a request ({type(exc).__name__})") from None
