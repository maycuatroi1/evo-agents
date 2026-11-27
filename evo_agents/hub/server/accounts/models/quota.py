"""What a read of a Z.ai account's quota found, and the shape POST /v1/provider-accounts/{name}/quota answers
(``evo_agents.hub.server.accounts.service.quota`` says how the hub reads and keeps it)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

ZAI_REASON = "quota used up, as Z.ai reported"


@dataclass
class Quota:
    """What a read of an account's quota found: the usage kept, or why none was."""

    ok: bool
    usage: dict | None = None
    status: int | None = None  # the endpoint's HTTP status; None when no answer came
    error: str | None = None


class QuotaWindow(BaseModel):
    utilization: float = Field(description="the share of the window's limit used, 0 to 1")
    resets_at: datetime | None = Field(default=None, description="when it resets, UTC")


class QuotaUsage(BaseModel):
    status: str = Field(description="allowed, or rejected once a window is used up")
    rate_limit_type: str | None = Field(default=None, description="with rejected: the window used up")
    resets_at: datetime | None = Field(default=None, description="with rejected: when that window resets")
    utilization: float | None = Field(default=None, description="the highest share used among the windows")
    windows: dict[str, QuotaWindow] = Field(default_factory=dict, description="five_hour and seven_day, as read")


class ProviderQuota(BaseModel):
    """What POST .../{name}/quota answers: whether the quota was read and kept, and an error without the key."""

    ok: bool
    usage: QuotaUsage | None = Field(default=None, description="the usage kept as the account's last")
    status: int | None = Field(default=None, description="Z.ai's HTTP status; null when no answer came")
    error: str | None = None
