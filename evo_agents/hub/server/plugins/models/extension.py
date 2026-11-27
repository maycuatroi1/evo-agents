"""The constants of the routes plugins contribute under /v1/x/<plugin>/ (``service.extension``): what the hub hands
a backend, what it serves of its answer, and the parameters of the paths."""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.protocol import NAME

MOUNT = "/v1/x"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_BODY = 1024 * 1024  # bytes of a request body the hub hands a backend
ROUTE_TIMEOUT = 30.0
FORWARDED_HEADERS = ("accept", "accept-language", "content-type", "if-modified-since", "if-none-match", "user-agent")
ANSWERED_HEADERS = ("cache-control", "content-language", "etag", "last-modified")
SAFE_TYPES = (
    "application/json",
    "application/octet-stream",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/plain",
)
SANDBOX = "default-src 'none'; frame-ancestors 'none'; sandbox"
PluginName = Annotated[str, Path(pattern=PLUGIN_ID)]
PROJECT_NAME = r"^[a-z0-9][a-z0-9-]{0,99}$"  # as evo_agents.hub.server.admin.PROJECT_NAME
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
RouteName = Annotated[str, Path(pattern=NAME["pattern"])]
