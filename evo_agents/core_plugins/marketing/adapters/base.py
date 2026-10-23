"""What every API adapter of the plugin marketing shares: the transport it calls out with (a fake in tests), what a
post that went out gives back, and why one failed.

An adapter sees the credential of its channel and the post, nothing else: the credential comes from the plugin's sealed
config at the moment of the call and goes into one request header, never into a log, a lease, an environment or a
prompt (``docs/marketing-plugin.md``). The default transport imports httpx when first used, so nothing here costs a
start. A test gives the adapters a fake transport: no test calls a platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

TIMEOUT = 20.0


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)  # holds the credential: never printed
    json: dict | None = None
    form: dict[str, str] | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: dict = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    async def send(self, request: HttpRequest) -> HttpResponse: ...


class HttpTransport:
    """The real one: one request through httpx."""

    async def send(self, request: HttpRequest) -> HttpResponse:
        import httpx

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            answer = await client.request(
                request.method, request.url, headers=request.headers, json=request.json, data=request.form
            )
        try:
            body = answer.json()
        except ValueError:
            body = {}
        return HttpResponse(
            answer.status_code, body if isinstance(body, dict) else {"items": body}, dict(answer.headers)
        )


@dataclass(frozen=True)
class Published:
    url: str  # where the post is
    external_id: str  # the platform's id of it
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Content:
    """What an adapter posts: the text, the account, the target of the account and the post a reply answers."""

    body: str
    account: str
    target: str | None = None
    reply_to: str | None = None
    title: str | None = None
    media_url: str | None = None


KINDS = ("locked", "rejected", "failed", "removed", "missing")


class AdapterError(Exception):
    """A post that did not go out. ``kind`` says what the breaker does: ``locked`` (a captcha, a checkpoint, a lock or a
    warning of the platform) opens it at once, ``rejected`` and ``failed`` count towards two in a row, ``removed`` is a
    post the platform took down, ``missing`` is no credential or no adapter (the post is a draft, no failure)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def check(answer: HttpResponse, what: str) -> dict:
    """The body of a good answer; AdapterError with the kind its status means otherwise. A message never holds the
    request, so it never holds the credential."""
    if answer.status < 300:
        return answer.body
    if answer.status in (401, 403) or answer.body.get("error") in ("checkpoint", "captcha", "locked"):
        raise AdapterError("locked", f"{what}: the platform refused the credential or locked the account")
    if answer.status == 429:
        raise AdapterError("failed", f"{what}: rate limited")
    kind = "rejected" if answer.status < 500 else "failed"
    raise AdapterError(kind, f"{what}: the platform answered {answer.status}")


class Adapter(Protocol):
    channel: str

    async def publish(self, content: Content, token: str) -> Published: ...
