"""The bounds of a request to /v1/memories and the cursors of its listings, for ``evo_agents.hub.server.memories``.

A request is read up to MAX_REQUEST bytes before FastAPI parses it (``BoundedRoute``). A cursor is the (updated_at, id)
of the last row a page examined, base64url JSON: it only positions a listing in rows the caller's filters select, so
it needs no signature.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute

from evo_agents.hub.memory import MAX_BODY

MAX_REQUEST = 6 * MAX_BODY + 64 * 1024  # a body escaped as JSON at worst (\u0001 for every byte), and the rest
MAX_ID = 2**63 - 1
TOO_LARGE = f"a request is at most {MAX_REQUEST // 1024} KiB, a memory at most {MAX_BODY // 1024} KiB"


class BoundedRoute(APIRoute):
    """Reads at most MAX_REQUEST bytes of a request before FastAPI parses it: a larger one is a 413, whether it says
    its length or not, and is never held in memory whole."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST):
                raise HTTPException(413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_REQUEST:
                    raise HTTPException(413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


def cursor(updated_at: datetime, memory_id: int) -> str:
    """The cursor of a listing after the row (``updated_at``, ``memory_id``)."""
    return base64.urlsafe_b64encode(json.dumps([updated_at.isoformat(), memory_id]).encode()).decode().rstrip("=")


def after(handed: str | None) -> tuple[str, int]:
    """The (updated_at, id) a cursor names, the start of a listing for None; 422 for one this hub did not hand out."""
    if handed is None:
        return "-infinity", 0
    try:
        moment, memory_id = json.loads(base64.urlsafe_b64decode(handed + "=" * (-len(handed) % 4)))
        if not isinstance(moment, str) or type(memory_id) is not int or not 0 <= memory_id <= MAX_ID:
            raise ValueError
        datetime.fromisoformat(moment)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(422, "cursor is not one this hub handed out: start the listing again without it") from None
    return moment, memory_id
