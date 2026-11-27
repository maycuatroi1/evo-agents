"""The shapes of installing a plugin from a market or a GitHub release and of asking for one
(``evo_agents.hub.server.market.service.install``)."""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, Field

from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugins.models.publishers import SIGNER
from evo_agents.hub.server.plugins.models.store import Plugin
from evo_agents.plugins.market import REPO

NOTICE = "market_install_request"  # evo_agents.hub.runs.NOTICE_KINDS
NOTHING_INSTALLED = "nothing was installed"
VERSION = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,40})?$"
TAG = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
BUNDLE_ASSET = re.compile(r"([a-z][a-z0-9-]{0,62})-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]{1,40})?)\.tar\.gz")
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (404, 409, 422, 502, 503)}


class MarketInstallIn(BaseModel):
    version: Annotated[str | None, Field(pattern=VERSION)] = Field(
        None, description="the version to install; the latest release when left out"
    )
    trust_publisher: bool = Field(
        False, description="trust the signer for the publisher from now on, when the hub does not yet"
    )


class GitHubInstallIn(BaseModel):
    repo: Annotated[str, Field(pattern=f"^{REPO.pattern}$", description="OWNER/REPO")]
    tag: Annotated[str | None, Field(pattern=TAG)] = Field(
        None, description="the release's tag; the latest if left out"
    )
    signer: Annotated[str | None, Field(pattern=SIGNER)] = Field(
        None, description="what signs the releases; sigstore:OWNER/REPO/.github/workflows/ci.yml when left out"
    )
    trust_publisher: bool = False


class MarketInstalled(BaseModel):
    plugin: Plugin
    trusted_publisher: bool = Field(description="whether this install made the hub trust the publisher's signer")
    upgraded: bool


class MarketRequestIn(BaseModel):
    project: Annotated[str, Field(pattern=PROJECT_NAME)]


class MarketRequest(BaseModel):
    id: int
    project: str
    plugin: str
    state: str
    created: bool = Field(description="False when the project had asked already and the request is still open")
