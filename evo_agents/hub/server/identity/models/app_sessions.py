"""The shapes and limits of app sessions and the apps that open them (``evo_agents.hub.server.app_sessions``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

HOST_PREFIX = "app:"  # the host of an app session's tokens row: app:<name>
SECRET_PREFIX = "evk_"
SECRET_BYTES = 32
SIGNATURE_HEADER = "X-Evo-App-Signature"
TIMESTAMP_HEADER = "X-Evo-App-Timestamp"
WINDOW = 300  # seconds a signature lives either side of the hub's clock
APP_NAME = r"^[a-z][a-z0-9-]{1,38}$"  # starts with a letter: ``_<app>`` is never the ``_<id>`` of auth's move_aside
HANDLE = r"^[A-Za-z0-9][A-Za-z0-9-]{0,59}$"  # the login part the app asks for; with ``_<app>``, 100 characters at most
REFUSED = "the app's signature does not hold: sign the request with the app's secret"


class AppSessionRequest(BaseModel):
    app: str = Field(pattern=APP_NAME, description="the name the app is registered under")
    subject: str = Field(
        min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$", description="the person's stable id at the app"
    )
    login: str = Field(pattern=HANDLE, description="the login the person gets on a first sign-in, before _<app>")


class AuthAppCreate(BaseModel):
    name: str = Field(pattern=APP_NAME)


class AuthApp(BaseModel):
    name: str
    created_by: str
    created_at: datetime
    revoked_at: datetime | None
    members: int = Field(description="the people it signed in at least once")


class AuthAppCreated(BaseModel):
    name: str
    secret: str = Field(description="the app's secret; shown once, the hub keeps it sealed")
    created_at: datetime
