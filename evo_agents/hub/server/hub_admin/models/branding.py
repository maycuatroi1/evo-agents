"""The shapes and limits of the branding of the hub (``evo_agents.hub.server.branding``)."""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import branding
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.errors import ErrorBody

IMAGE_KIND = "branding-image"
MAX_IMAGE_BYTES = 256 * 1024
CACHED = 8
ICO = b"\x00\x00\x01\x00"
UNAVAILABLE = "the blob store did not answer; the branding was not changed, try again shortly"
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422)}
IMAGE_REFUSALS = {code: {"model": ErrorBody} for code in (409, 413, 422, 503)}
Asset = Literal[branding.ASSETS]


class BrandingColors(BaseModel):
    light: dict[str, str] = Field(default_factory=dict, description="token -> #rrggbb, for the light theme")
    dark: dict[str, str] = Field(default_factory=dict, description="token -> #rrggbb, for the dark theme")


class Branding(BaseModel):
    """The branding as the web applies it; what is unset is null or empty, and the web keeps its own there."""

    name: str | None = Field(description="the product's name")
    logo_url: str | None = Field(description="GET of the logo on this hub, in place of the web's mark")
    favicon_url: str | None = Field(description="GET of the favicon on this hub")
    colors: BrandingColors
    version: int = Field(description="the version of the branding; 0 while it was never set")


class BrandingImage(StrictModel):
    data: str = Field(
        min_length=1,
        max_length=(MAX_IMAGE_BYTES + 2) // 3 * 4,
        description=f"the image, base64: PNG, JPEG or WebP (or ICO for the favicon), at most {MAX_IMAGE_BYTES} bytes",
    )
    if_version: int | None = Field(None, ge=0, description="409 unless the branding is at this version now")

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("not base64") from None
        return value
