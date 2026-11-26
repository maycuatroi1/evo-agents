"""The notification settings of a member: the notices they turned off, the tier of each kind of notification, quiet
hours, the time zone and the time of the summary push (GET and PUT /v1/me/notification-settings)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import curator, runs
from evo_agents.hub.server.push.models import KINDS
from evo_agents.hub.tables_push import TIERS


class NoticeSetting(BaseModel):
    kind: Literal[runs.MUTABLE_NOTICE_KINDS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="a kind of notice a member may turn off"
    )
    on: bool = Field(description="false: the member gets no notice of this kind, on any channel")


HIDE_DETAILS = (
    "push notifications carry only the kind of thing, the project and the link, as on a locked screen anyone may read"
)
TIER = (
    "interrupt: pushed to the member's devices; brief: only in Today and the Inbox; off: in the Inbox alone. The "
    "Inbox, the bell and the channels get every notification whatever its tier"
)
DEFAULT_TIER = (
    "false: the hub's default, interrupt for a decision asked of the member, a run_failed of a run they dispatched "
    "(not one the Curator's night shift queued), a release that went wrong and merge_paused, and brief for every "
    "other; a tier the member chose holds for every notification of the kind"
)


class TierSetting(BaseModel):
    kind: Literal[KINDS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="a kind of notification: decision, proposal or a notice's"
    )
    tier: Literal[TIERS] = Field(description=TIER)  # pyright: ignore[reportInvalidTypeForm]
    chosen: bool = Field(description=DEFAULT_TIER)


class TierChoice(BaseModel):
    kind: Literal[KINDS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="a kind of notification: decision, proposal or a notice's"
    )
    tier: Literal[TIERS] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        description=f"{TIER}; null: back to the hub's default"
    )


class QuietHours(BaseModel):
    start: str = Field(pattern=curator.TIME_OF_DAY, description="HH:MM, 24 hours, in the member's time zone")
    end: str = Field(
        pattern=curator.TIME_OF_DAY,
        description="HH:MM, 24 hours; the quiet hours run past midnight when it is not after start",
    )

    @model_validator(mode="after")
    def _not_empty(self):
        if self.start == self.end:
            raise ValueError("quiet hours end at another time than they start")
        return self


class NotificationSettings(BaseModel):
    notices: list[NoticeSetting] = Field(description="each kind of notice a member may turn off, and whether it is on")
    hide_details: bool = Field(False, description=HIDE_DETAILS)
    tiers: list[TierSetting] = Field(description="each kind of notification and its tier")
    quiet_hours: QuietHours | None = Field(
        description="when an interrupt waits to be pushed until they end, a decision's too; null: none"
    )
    time_zone: str = Field(description="the IANA time zone of the quiet hours and digest_at")
    time_zone_chosen: bool = Field(description="false: the hub's time zone, EVO_HUB_WEB_TIME_ZONE")
    digest_at: str = Field(description="the local time of the summary push, HH:MM")


class NotificationSettingsIn(BaseModel):
    notices: list[NoticeSetting] = Field(
        default_factory=list,
        max_length=len(runs.MUTABLE_NOTICE_KINDS),
        description="the kinds to set; the others stay",
    )
    hide_details: bool | None = Field(None, description=f"{HIDE_DETAILS}; unchanged when left out")
    tiers: list[TierChoice] = Field(
        default_factory=list, max_length=len(KINDS), description="the kinds to set; the others stay"
    )
    quiet_hours: QuietHours | None = Field(None, description="null: none; unchanged when left out")
    time_zone: str | None = Field(
        None,
        max_length=curator.MAX_TIME_ZONE_CHARS,
        pattern=curator.TIME_ZONE,
        description="an IANA name such as Asia/Tokyo; null: the hub's; unchanged when left out",
    )
    digest_at: str | None = Field(
        None, pattern=curator.TIME_OF_DAY, description="HH:MM; null: the hub's default; unchanged when left out"
    )

    @field_validator("notices")
    @classmethod
    def _once(cls, value: list[NoticeSetting]) -> list[NoticeSetting]:
        if len({setting.kind for setting in value}) != len(value):
            raise ValueError("name each kind of notice once")
        return value

    @field_validator("tiers")
    @classmethod
    def _tier_once(cls, value: list[TierChoice]) -> list[TierChoice]:
        if len({choice.kind for choice in value}) != len(value):
            raise ValueError("name each kind of notification once")
        return value

    @model_validator(mode="after")
    def _something(self):
        named = self.model_fields_set & {"quiet_hours", "time_zone", "digest_at"}
        if not (self.notices or self.tiers or named or self.hide_details is not None):
            raise ValueError("set notices, hide_details, tiers, quiet_hours, time_zone or digest_at")
        return self

    def preferences(self) -> dict:
        """The columns of notification_preferences this body sets (``push_tiers.set_preferences``)."""
        values: dict = {}
        if self.hide_details is not None:
            values["hide_details"] = self.hide_details
        if self.tiers:
            values["tiers"] = {choice.kind: choice.tier for choice in self.tiers}
        if "quiet_hours" in self.model_fields_set:
            quiet = self.quiet_hours
            values["quiet_start"], values["quiet_end"] = (quiet.start, quiet.end) if quiet else (None, None)
        for name in ("time_zone", "digest_at"):
            if name in self.model_fields_set:
                values[name] = getattr(self, name)
        return values

    def shown(self) -> str:
        """What the audit row says of this body."""
        chosen = [f"{setting.kind}={'on' if setting.on else 'off'}" for setting in self.notices]
        if self.hide_details is not None:
            chosen.append(f"hide_details={'on' if self.hide_details else 'off'}")
        chosen += [f"tier:{choice.kind}={choice.tier or 'default'}" for choice in self.tiers]
        if "quiet_hours" in self.model_fields_set:
            quiet = self.quiet_hours
            chosen.append(f"quiet_hours={quiet.start}-{quiet.end}" if quiet else "quiet_hours=none")
        for name in ("time_zone", "digest_at"):
            if name in self.model_fields_set:
                chosen.append(f"{name}={getattr(self, name) or 'default'}")
        return " ".join(chosen)
