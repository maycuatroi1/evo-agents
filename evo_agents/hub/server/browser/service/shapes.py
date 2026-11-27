"""The body that creates a browser identity (``routes.identities``, and a login on the worker's own machine): a shape
of the service, since the pattern of its worker's name comes from ``worker_models``, which reaches the service through
``admin``, so the models layer may not import it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import browser_login, profiles
from evo_agents.hub.server.browser.models.identities import MAX_DOMAINS, OWNERS, Domain, IdentityName
from evo_agents.hub.server.worker_models import WORKER_NAME


class BrowserIdentityCreate(BaseModel):
    name: IdentityName
    domains: list[Domain] = Field(
        min_length=1,
        max_length=MAX_DOMAINS,
        description="the sites whose cookies it keeps, each with its subdomains; the others are dropped at every save",
    )
    worker: str = Field(
        pattern=WORKER_NAME,
        description="the worker that holds it: your own, or a shared one, serving the project; its profile opens there "
        "alone",
    )
    owner: Literal[OWNERS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        "member",
        description="member: yours alone (the writer role); project: the admins', usable by every writer; agent: an "
        "agent profile's (``agent``), the admins' to manage, which the profile hands to its runs",
    )
    agent: str | None = Field(
        None,
        pattern=profiles.HANDLE_PATTERN,
        description="the handle of the agent profile that owns it, @ left out; required for owner agent, refused "
        "otherwise. The first identity of a profile is the one it hands its runs",
    )
    login_url: str | None = Field(
        None,
        max_length=browser_login.MAX_URL_CHARS,
        description="the page a login session opens, an http or https URL; null: the home page of the first domain",
    )
