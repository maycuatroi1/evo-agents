"""The token of a run to the hub's gateway route (``service.leases``): an HMAC of its lease's id under its key of the
gateway, so the hub derives it again for each ask and stores only its SHA-256."""

from __future__ import annotations

import base64
import hashlib
import hmac

from evo_agents.hub.server.gateway.models.leases import TOKEN_PREFIX


def run_token(key: str, lease_id: int) -> str:
    """The token of the run whose key of the gateway is ``key``, leased as ``lease_id``."""
    mac = hmac.new(key.encode(), f"evo-gateway-run:{lease_id}".encode(), hashlib.sha256).digest()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
