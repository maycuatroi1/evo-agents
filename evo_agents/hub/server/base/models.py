"""The base of a request model: a field the API does not name is refused (422), never ignored; and the names a request
model checks, which import nothing of the server so that a package's models stay its lowest layer."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

LOGIN_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"  # a member's login, as the users table accepts it
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="hex SHA-256 of the bytes")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
