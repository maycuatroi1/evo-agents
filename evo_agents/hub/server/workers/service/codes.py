"""How the hub keeps a pairing code (its HMAC under the session secret, 503 without one) and counts the joins it
refuses per client address (``RefusalLimit``)."""

from __future__ import annotations

import time

from fastapi import HTTPException, Request

from evo_agents.hub import cache
from evo_agents.hub.server.security import keyed_digest
from evo_agents.hub.server.workers.models.codes import (
    JOIN_REFUSALS,
    JOIN_WINDOW,
    MAX_JOIN_CLIENTS,
    NO_SECRET,
    PAIRING_PURPOSE,
)


def code_hash(secret: str, code: str) -> str:
    """How the hub keeps a code: its HMAC under the session secret. A plain hash next to the selector in clear would
    let whoever reads worker_pairings try the 32**4 rests of the code offline."""
    return keyed_digest(secret, PAIRING_PURPOSE, code)


def pairing_secret(request: Request) -> str:
    secret = request.app.state.config.session_secret
    if not secret:
        raise HTTPException(503, NO_SECRET)
    return secret


class RefusalLimit(cache.LocalWindow):
    """Refused joins per client address in a sliding window, in this process's memory (``cache.LocalWindow``): an
    address refused JOIN_REFUSALS times within JOIN_WINDOW waits until the oldest of those refusals leaves the window.
    The app counts on Redis instead when the cache is shared (``app.state.join_limit``, a ``cache.Window`` over it)."""

    def __init__(
        self,
        limit: int = JOIN_REFUSALS,
        window: float = JOIN_WINDOW,
        max_clients: int = MAX_JOIN_CLIENTS,
        clock=time.monotonic,
    ):
        super().__init__(limit, window, max_clients, clock)

    def refuse(self, client: str) -> None:
        """Count one refusal of ``client``."""
        self.hit(client)


def client_address(request: Request) -> str:
    """Where a request came from: the peer's address, or the client's that a trusted proxy forwarded (uvicorn's
    forwarded_allow_ips, set by EVO_HUB_FORWARDED_ALLOW_IPS)."""
    return request.client.host if request.client else "unknown"
