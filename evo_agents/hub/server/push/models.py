"""The models of push notifications: the limits and patterns of a device, the reason a device went, the tiers of
notifications (KINDS, DEFAULT_INTERRUPTS) and what the hub's default tier depends on (Facts), and the API's shapes of
a member's devices, a test push and what a client needs to subscribe. They import only ``webpush``, the crypto a Web
Push subscription is checked with."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import runs
from evo_agents.hub.server.push import webpush
from evo_agents.hub.server.runs.models import LINE, MAX_ID
from evo_agents.hub.tables_push import APNS_ENVIRONMENTS, MAX_ADDRESS_CHARS, MAX_LABEL_CHARS
from evo_agents.hub.tables_push import KINDS as DEVICE_KINDS

# Devices

MAX_ERROR_CHARS = 500  # of why a test push did not go
APNS_TOKEN = r"^[0-9A-Fa-f]{64,200}$"  # a device token as an app gets it, in hex
APNS_TOPIC = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"  # the app's bundle id, maybe with a suffix such as .voip
FCM_TOKEN = rf"^[A-Za-z0-9_:-]{{20,{MAX_ADDRESS_CHARS}}}$"  # a registration token of Firebase Cloud Messaging
MAX_DEVICES = 20  # a member's devices; adding one more is refused until one goes
# Why a delivery to a device failed, and the device went with it, when the token that added it no longer lives.
SIGN_IN_ENDED = (
    "the sign-in that added this device ended (signed out, revoked or expired): turn push on again on the device"
)

# Tiers and quiet hours

KINDS = ("decision", "proposal", *runs.NOTICE_KINDS)  # the kinds of notification a member sets a tier for
# The kinds whose default tier is interrupt, for the notifications ``default_tier`` names: the rest default to brief.
DEFAULT_INTERRUPTS = ("decision", "run_failed", "release", "merge_paused")
RELEASED = "published"  # the event of a release notice of a release that went out (``release_watch``)
FALLBACK_ZONE = "UTC"  # the time zone of quiet hours whose own Postgres no longer knows (logged)
PREFERENCES = ("tiers", "quiet_start", "quiet_end", "time_zone", "digest_at")  # the columns ``set_preferences`` sets


@dataclass(frozen=True)
class Facts:
    """What the hub's default tier of one notification to one member depends on."""

    kind: str  # kind_of the notification
    asked_of_member: bool = False  # a decision the member answers
    dispatched_by_member: bool = False  # the notification's run was dispatched by the member
    run_kind: str | None = None
    event: str | None = None  # details["event"] of a notice


# The API

DeviceId = Annotated[int, Path(ge=1, le=MAX_ID)]
Label = Annotated[
    str | None,
    Field(min_length=1, max_length=MAX_LABEL_CHARS, pattern=LINE, description="the member's name for it"),
]


class WebPushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=200, description="the browser's P-256 public key, base64url")
    auth: str = Field(min_length=1, max_length=100, description="the subscription's 16-byte secret, base64url")

    @field_validator("p256dh")
    @classmethod
    def _point(cls, value: str) -> str:
        webpush.subscriber_key(value)
        return value

    @field_validator("auth")
    @classmethod
    def _secret(cls, value: str) -> str:
        webpush.auth_secret(value)
        return value


class WebPushSubscription(BaseModel):
    """A browser's subscription, as ``PushSubscription.toJSON()`` gives it; expirationTime is not kept."""

    endpoint: str = Field(min_length=1, max_length=webpush.MAX_ENDPOINT_CHARS, description="a push service's URL")
    keys: WebPushKeys

    @field_validator("endpoint")
    @classmethod
    def _push_service(cls, value: str) -> str:
        problem = webpush.endpoint_problem(value) if value.isascii() else "an endpoint is an ASCII https URL"
        if problem is not None:
            raise ValueError(problem)
        return value


class WebPushDeviceIn(BaseModel):
    kind: Literal["webpush"]
    label: Label = None
    subscription: WebPushSubscription


class ApnsDeviceIn(BaseModel):
    kind: Literal["apns"]
    label: Label = None
    token: str = Field(pattern=APNS_TOKEN, description="the device token, in hex")
    topic: str = Field(pattern=APNS_TOPIC, description="the app's bundle id")
    environment: Literal[APNS_ENVIRONMENTS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="the APNs servers the token belongs to"
    )


class FcmDeviceIn(BaseModel):
    kind: Literal["fcm"]
    label: Label = None
    token: str = Field(pattern=FCM_TOKEN, description="the registration token")


DeviceIn = Annotated[WebPushDeviceIn | ApnsDeviceIn | FcmDeviceIn, Field(discriminator="kind")]


class PushDevice(BaseModel):
    id: int
    kind: Literal[DEVICE_KINDS]  # pyright: ignore[reportInvalidTypeForm]
    label: str | None
    created_at: datetime
    last_sent_at: datetime | None = Field(description="when a push last reached it; null before the first")
    this_session: bool = Field(description="the sign-in this request was made with added it")


class PushDeviceList(BaseModel):
    devices: list[PushDevice] = Field(description="the caller's devices that push reaches, newest first")


class PushTestResult(BaseModel):
    sent: bool = Field(description="the push service took the test push")
    removed: bool = Field(description="the push service no longer knows the device, so it was removed")
    error: str | None = Field(description="why the test push did not go")


class PushSenders(BaseModel):
    webpush: bool = Field(description="Web Push to browsers: the hub has its VAPID key")
    apns: bool = Field(description="APNs to iPhones and iPads")
    fcm: bool = Field(description="Firebase Cloud Messaging to Android phones")


class PushConfig(BaseModel):
    vapid_public_key: str | None = Field(
        description="the applicationServerKey a browser subscribes with, base64url; null while Web Push is off"
    )
    senders: PushSenders = Field(description="which kinds of push device the hub sends to now")
