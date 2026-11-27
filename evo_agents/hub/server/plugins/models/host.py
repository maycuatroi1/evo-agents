"""The shapes of what the plugin host pulls and reports (``service.host``), and the states of a backend."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

HOST_ROUTES = "/internal/plugin-host"  # outside /v1: the reverse proxy never sends it to the api
BACKEND_STATES = ("placing", "starting", "running", "backoff")
ISOLATIONS = ("uid", "process")
MAX_ERROR_CHARS = 4000  # plugin_backends.error


class BackendPlugin(BaseModel):
    name: str
    version: str
    tier: str
    sha256: str = Field(description="the bundle's SHA-256; the host fetches it from the blob store")
    size: int
    backend: dict = Field(description="the manifest's backend: runtime, entry, requirements")
    init: dict = Field(description="the initialize payload, config secrets decrypted; the host adds the token")


class BackendToken(BaseModel):
    token: str = Field(description="a fresh evp_ token for the backend about to start; the plugin's older ones revoked")


class BackendStatusIn(BaseModel):
    plugin: str
    version: str
    state: str
    isolation: str
    pid: int | None = None
    restarts: int = 0
    error: str | None = None


class BackendStatus(BaseModel):
    plugin: str
    version: str | None
    state: str | None
    isolation: str | None
    pid: int | None
    restarts: int | None
    error: str | None
    started_at: datetime | None
    retry_at: datetime | None
    reported_at: datetime | None
