"""The path and refusals of the routes of the provider accounts of an agent; what a write takes is in
``evo_agents.hub.server.agent_accounts.service.shapes``."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from evo_agents.hub.server.errors import ErrorBody

PATH = "/{project}/agents/{handle}/provider-accounts"
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}
Models = list[Annotated[str, Field(min_length=1, max_length=200)]]
