"""The shapes of the profile of a run's browser identity, to and from its worker (``routes.leases``): the lease, the
save, the worker's word that a profile needs a new login, the identity of a run as the service found it, and the route
class that bounds an uploaded archive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Path, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from evo_agents.hub.server.browser.models.identities import STATUSES
from evo_agents.hub.server.browser.models.profiles import MAX_ARCHIVE_BYTES
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.runs.models import MAX_ID

CAUSES = ("unopenable", "newer_chromium")  # what a worker says; the hub sets it for a profile it cannot open too
NO_KEY = "this hub opens no browser profile: it needs {missing}, the key that seals them (docs/credentials.md)"
TOO_LARGE = f"a browser profile is at most {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB packed; nothing was saved"
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
IdentityId = Annotated[int, Path(ge=1, le=MAX_ID, description="one of the run's browser identities")]


class ProfileRoute(APIRoute):
    """Reads at most MAX_ARCHIVE_BYTES of a request before FastAPI parses it: a larger one gets 413, whether it says
    its length or not, and is never held in memory whole."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_ARCHIVE_BYTES):
                return error_response(request, 413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    return error_response(request, 413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


class BrowserProfileAsk(BaseModel):
    identity_id: int = Field(ge=1, le=MAX_ID, description="one of the run's browser_identities")


class BrowserProfileLease(BaseModel):
    identity_id: int
    name: str
    domains: list[str] = Field(
        description="the sites whose cookies the profile keeps, each with its subdomains; the worker drops every "
        "other cookie before it saves the profile"
    )
    status: Literal[STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    version: int = Field(description="the version of the profile handed out, the base_version of its save; 0: none")
    chromium_version: str | None = Field(description="the Chromium that saved it, as it said; null for none")
    profile: str | None = Field(
        description="the archive of the profile in base64: a gzipped tar of the profile directory without its "
        "caches; null for one never saved, or one the hub could not open, which sets the identity needs_login; null "
        "too for a login session on another worker than the identity's (a Move)"
    )
    login_url: str = Field(description="the page a login session opens: the identity's, else its first domain's")


class BrowserProfileSaved(BaseModel):
    identity_id: int
    version: int = Field(description="the version the profile is at now, the one saved")


class BrowserNeedsLogin(BaseModel):
    cause: Literal[CAUSES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="unopenable: the worker's Chromium could not open the profile; newer_chromium: a Chromium newer "
        "than the worker's saved it"
    )


class BrowserIdentityState(BaseModel):
    identity_id: int
    status: Literal[STATUSES]  # pyright: ignore[reportInvalidTypeForm]


@dataclass(frozen=True)
class Found:
    """A browser identity of a run that the run's worker may open (``service.leases.usable_identity``)."""

    run_id: int
    project_id: int
    project: str
    dispatched_by: int
    identity: Any  # the identity's row
    worker_id: int = 0  # the worker that holds the run
    login: bool = False  # the run is a login session (``browser_login``)

    @property
    def moves(self) -> bool:
        """A login session on another worker than the identity's: it starts on an empty profile and, saved, takes the
        identity to its worker."""
        return self.login and self.identity.worker_id != self.worker_id
