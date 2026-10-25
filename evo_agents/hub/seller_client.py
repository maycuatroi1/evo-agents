"""How a buying hub talks to the hub of a plugin's seller (plan plugin-market, criterion a7): the seller's API under
``/v1/seller-api`` at the ``url`` a market's index lists for it (``evo_agents.hub.server.seller_api``).

- ``place_order``: POST /v1/seller-api/orders with the plugin, the buying hub's stable id and address, the months and a
  contact; the seller answers the order's number, its secret (which the buyer seals and the seller keeps only the
  SHA-256 of), the total and how to pay it;
- ``order``: GET /v1/seller-api/orders/{number} with the secret in X-Evo-Order-Secret: its state and, once paid, the
  licence's token;
- ``bundle``: GET /v1/seller-api/bundles/{plugin}[/{version}] with ``Authorization: License <token>`` and the hub's
  id in X-Evo-Hub-Id: the signed bundle, in base64, with its signature.

The client never logs a secret or a token, and the messages of SellerUnavailable name none. Blocking: the hub runs it
in a thread. Standard library and httpx only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

import httpx

from evo_agents import __version__

API = "/v1/seller-api"
ORDER_SECRET_HEADER = "X-Evo-Order-Secret"
HUB_ID_HEADER = "X-Evo-Hub-Id"
LICENSE_SCHEME = "License"
TIMEOUT_SECONDS = 20.0
MAX_ANSWER = 48 * 1024 * 1024  # a bundle of 32 MiB in base64, with its signature


class SellerUnavailable(Exception):
    """The seller did not answer, or answered what the hub cannot use; the message is safe to show. ``status`` is the
    seller's HTTP status when it answered one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SellerClient:
    """The seller API of the hub at ``url``; ``transport`` stands in for the network in tests."""

    def __init__(self, url: str, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        headers = {"User-Agent": f"evo-agents-hub/{__version__}", "Accept": "application/json"}
        self._http = httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS), headers=headers, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SellerClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, method: str, path: str, doing: str, **kwargs) -> dict:
        try:
            with self._http.stream(method, f"{self.url}{API}{path}", **kwargs) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_ANSWER:
                        raise SellerUnavailable(f"the seller at {self.url} answered too much while {doing}")
                status = response.status_code
        except httpx.TimeoutException:
            raise SellerUnavailable(f"the seller at {self.url} did not answer in time while {doing}") from None
        except httpx.HTTPError as exc:
            raise SellerUnavailable(
                f"the seller at {self.url} could not be reached while {doing}: {type(exc).__name__}"
            ) from None
        try:
            data = httpx.Response(status, content=bytes(body)).json()
        except ValueError:
            data = None
        if status >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            said = f": {str(detail)[:300]}" if isinstance(detail, str) else ""
            raise SellerUnavailable(f"the seller at {self.url} answered {status} while {doing}{said}", status)
        if not isinstance(data, dict):
            raise SellerUnavailable(f"the seller at {self.url} answered no JSON object while {doing}", status)
        return data

    def info(self) -> dict:
        return self._call("GET", "/info", "reading the seller")

    def place_order(self, plugin: str, hub: str, hub_url: str | None, months: int, contact: dict) -> dict:
        body = {"plugin": plugin, "hub": hub, "hub_url": hub_url, "months": months, "contact": contact}
        return self._call("POST", "/orders", f"ordering {plugin}", json=body)

    def order(self, number: str, secret: str) -> dict:
        return self._call("GET", f"/orders/{number}", f"reading order {number}", headers={ORDER_SECRET_HEADER: secret})

    def bundle(self, plugin: str, token: str, hub: str, version: str | None = None) -> tuple[dict, bytes]:
        """The seller's answer for the bundle of ``plugin`` (its latest version, or ``version``) and its bytes,
        checked against the SHA-256 and size the seller gives."""
        path = f"/bundles/{plugin}" + (f"/{version}" if version else "")
        headers = {"Authorization": f"{LICENSE_SCHEME} {token}", HUB_ID_HEADER: hub}
        found = self._call("GET", path, f"downloading {plugin}", headers=headers)
        try:
            data = base64.b64decode(found["bundle"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise SellerUnavailable(f"the seller at {self.url} sent no bundle of {plugin} in base64") from None
        if hashlib.sha256(data).hexdigest() != found.get("sha256") or len(data) != found.get("size"):
            raise SellerUnavailable(f"the bundle of {plugin} the seller at {self.url} sent is not the one it names")
        return found, data
