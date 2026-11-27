"""The body of a login session of a browser identity (``routes.logins``): where it runs, for a Move."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BrowserLogin(BaseModel):
    worker: str | None = Field(
        None,
        description="a Move: the name of the worker the identity should live on after the session saves (your own, "
        "or a shared one that serves the project and has the feature browser); null: the worker that holds it",
    )
