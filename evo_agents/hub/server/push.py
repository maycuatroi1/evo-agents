"""The routes of push notifications: a member's push devices, a test push to one, and what a client needs to subscribe.
``evo_agents.hub.server.push_store`` keeps the devices, ``push_senders`` sends to them through the outbox
(``notifications``), and ``docs/notifications.md`` is the design.

POST /v1/me/push/devices adds a device of the caller's, bound to the token they called with: a browser's Web Push
subscription as ``PushSubscription.toJSON()`` gives it (its endpoint must be a push service's, its keys a P-256 key
and a 16-byte secret), an APNs token with its topic and environment, or an FCM token. Adding a device the caller has
refreshes it; one another member had moves to the caller. A member has at most ``push_store.MAX_DEVICES`` (409).
Audited as push_device.add, naming the device's id and kind, never its endpoint or token. GET /v1/me/push/devices
lists the caller's devices that reach them (kind, label, when added, when a push last reached it, and whether this
sign-in added it). DELETE /v1/me/push/devices/{id} removes one (push_device.remove). POST
/v1/me/push/devices/{id}/test pushes a test notification to it now and says whether it went; a device its push service
no longer knows is removed, and a kind whose sender is off answers 503.

GET /v1/push/config, without a sign-in, gives the public VAPID key browsers subscribe with (null while Web Push is off)
and which senders are on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import push_payload
from evo_agents.hub.server import audit, push_store, webpush
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import SEND_TIMEOUT_SECONDS, ChannelGone, channel_class
from evo_agents.hub.server.push_senders import PushSender, device_config, senders_on
from evo_agents.hub.server.runs.models import LINE, MAX_ID
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_push import APNS, APNS_ENVIRONMENTS, KINDS, MAX_ADDRESS_CHARS, MAX_LABEL_CHARS, WEBPUSH

log = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500  # of why a test push did not go
APNS_TOKEN = r"^[0-9A-Fa-f]{64,200}$"  # a device token as an app gets it, in hex
APNS_TOPIC = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"  # the app's bundle id, maybe with a suffix such as .voip
FCM_TOKEN = rf"^[A-Za-z0-9_:-]{{20,{MAX_ADDRESS_CHARS}}}$"  # a registration token of Firebase Cloud Messaging

config_router = APIRouter(prefix="/v1/push", tags=["push"])
router = APIRouter(prefix="/v1/me/push", tags=["push"], responses={401: {"model": ErrorBody}})

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
    environment: Literal[APNS_ENVIRONMENTS] = Field(description="the APNs servers the token belongs to")


class FcmDeviceIn(BaseModel):
    kind: Literal["fcm"]
    label: Label = None
    token: str = Field(pattern=FCM_TOKEN, description="the registration token")


DeviceIn = Annotated[WebPushDeviceIn | ApnsDeviceIn | FcmDeviceIn, Field(discriminator="kind")]


class PushDevice(BaseModel):
    id: int
    kind: Literal[KINDS]
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


def _address(body) -> tuple[str, dict]:
    """The address and the params of the device ``body`` describes, as push_devices keeps them."""
    if body.kind == WEBPUSH:
        keys = body.subscription.keys
        return body.subscription.endpoint, {"p256dh": keys.p256dh, "auth": keys.auth}
    if body.kind == APNS:
        return body.token.lower(), {"topic": body.topic, "environment": body.environment}
    return body.token, {}


def _device(row, token_id: int) -> PushDevice:
    return PushDevice(
        id=row.id,
        kind=row.kind,
        label=row.label,
        created_at=row.created_at,
        last_sent_at=row.last_sent_at,
        this_session=row.token_id == token_id,
    )


def _said(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:MAX_ERROR_CHARS]


async def _record(conn, user: CurrentUser, action: str, device_id: int, kind: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"push_device:{device_id} kind={kind} user={user.login} by=member",
    )


@config_router.get("/config", response_model=PushConfig)
async def push_config(request: Request) -> PushConfig:
    """The public VAPID key and the senders that are on; no sign-in needed."""
    config = request.app.state.config
    senders = senders_on(config)
    key = webpush.public_key(config.vapid_private_key) if senders[WEBPUSH] else None
    return PushConfig(vapid_public_key=key, senders=PushSenders(**senders))


@router.post(
    "/devices",
    status_code=201,
    response_model=PushDevice,
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def add_device(request: Request, body: Annotated[DeviceIn, Body()], user: CurrentUser) -> PushDevice:
    """Add a push device of the caller's, or refresh it."""
    address, params = _address(body)
    async with request.app.state.engine.begin() as conn:
        added = await push_store.add_device(
            conn,
            user_id=user.user_id,
            token_id=user.token_id,
            kind=body.kind,
            address=address,
            params=params,
            label=body.label,
        )
        if added is None:
            raise HTTPException(
                409, f"you have {push_store.MAX_DEVICES} push devices already: remove one before adding another"
            )
        row, new = added
        if new:
            await _record(conn, user, audit.PUSH_DEVICE_ADD, row.id, body.kind)
    log.info("push device added", extra={"login": user.login, "kind": body.kind, "new": new})
    return _device(row, user.token_id)


@router.get("/devices", response_model=PushDeviceList)
async def list_devices(request: Request, user: CurrentUser) -> PushDeviceList:
    """The caller's push devices that push reaches, newest first."""
    async with request.app.state.engine.begin() as conn:
        rows = await push_store.devices_of(conn, user.user_id)
    return PushDeviceList(devices=[_device(row, user.token_id) for row in rows])


@router.delete("/devices/{device_id}", status_code=204, responses={404: {"model": ErrorBody}})
async def remove_device(request: Request, device_id: DeviceId, user: CurrentUser) -> Response:
    """Remove a push device of the caller's; the pushes waiting for it go with it."""
    async with request.app.state.engine.begin() as conn:
        removed = await push_store.remove_device(conn, device_id, user.user_id)
        if removed is None:
            raise HTTPException(404, f"you have no push device {device_id}")
        await _record(conn, user, audit.PUSH_DEVICE_REMOVE, device_id, removed[1])
    log.info("push device removed", extra={"login": user.login, "kind": removed[1]})
    return Response(status_code=204)


def _sender(kind: str, config) -> PushSender:
    """An instance of the sender of devices of ``kind``; 503 when the hub sends to none of them now."""
    cls = channel_class(kind)
    if cls is None or not issubclass(cls, PushSender):
        raise HTTPException(503, f"this hub does not send to {kind} devices yet")
    missing = cls.missing(config)
    if missing:
        raise HTTPException(503, f"this hub does not send to {kind} devices: set {', '.join(missing)}")
    return cls(config)


@router.post(
    "/devices/{device_id}/test",
    response_model=PushTestResult,
    responses={404: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def try_device(request: Request, device_id: DeviceId, user: CurrentUser) -> PushTestResult:
    """Push a test notification to one of the caller's devices now."""
    engine, config = request.app.state.engine, request.app.state.config
    async with engine.begin() as conn:
        device = await push_store.device_of(conn, user.user_id, device_id)
    if device is None:
        raise HTTPException(404, f"you have no push device {device_id}")
    sender = _sender(device.kind, config)
    trial = push_payload.trial_payload(getattr(config, "public_url", None))
    try:
        await asyncio.wait_for(
            sender.deliver(trial, device_config(device.address, device.params)), SEND_TIMEOUT_SECONDS
        )
    except ChannelGone as exc:
        async with engine.begin() as conn:
            if await push_store.remove_device(conn, device.id, user.user_id) is not None:
                await _record(conn, user, audit.PUSH_DEVICE_REMOVE, device.id, device.kind)
        log.info("push device gone on a test", extra={"login": user.login, "kind": device.kind})
        return PushTestResult(sent=False, removed=True, error=_said(exc))
    except Exception as exc:  # whatever the push service or the network did is the answer, not a 500
        log.info("test push not sent", extra={"login": user.login, "kind": device.kind}, exc_info=True)
        return PushTestResult(sent=False, removed=False, error=_said(exc))
    async with engine.begin() as conn:
        await push_store.sent(conn, device.id)
    return PushTestResult(sent=True, removed=False, error=None)
