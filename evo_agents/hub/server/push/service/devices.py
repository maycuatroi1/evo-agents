"""What the routes of push do with a member's devices, bound to the token they called with: add one (a browser's Web
Push subscription, an APNs token with its topic and environment, or an FCM token; one the caller has is refreshed, one
another member had moves to the caller; at most ``models.MAX_DEVICES``, else 409), list those that reach them, remove
one, and push a test notification to one now. A device its push service no longer knows is removed on a test, and a
kind whose sender is off answers 503. Adding and removing are audited as push_device.add and push_device.remove,
naming the device's id and kind, never its endpoint or token. ``push_config`` says what a client needs to subscribe.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from evo_agents.hub import push_payload
from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications.models import SEND_TIMEOUT_SECONDS, ChannelGone
from evo_agents.hub.server.notifications.service.channels import channel_class
from evo_agents.hub.server.push import repository, webpush
from evo_agents.hub.server.push.models import (
    MAX_DEVICES,
    MAX_ERROR_CHARS,
    PushConfig,
    PushDevice,
    PushDeviceList,
    PushSenders,
    PushTestResult,
)
from evo_agents.hub.server.push.service.senders import PushSender, device_config, senders_on
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_push import APNS, WEBPUSH

log = logging.getLogger(__name__)


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


async def _record(conn, user: Principal, action: str, device_id: int, kind: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"push_device:{device_id} kind={kind} user={user.login} by=member",
    )


def push_config(config) -> PushConfig:
    """The public VAPID key and the senders that are on, on a hub configured with ``config``."""
    senders = senders_on(config)
    key = webpush.public_key(config.vapid_private_key) if senders[WEBPUSH] else None
    return PushConfig(vapid_public_key=key, senders=PushSenders(**senders))


async def add_device(engine, user: Principal, body) -> PushDevice:
    """Add the push device ``body`` describes to ``user``'s, or refresh it."""
    address, params = _address(body)
    async with engine.begin() as conn:
        added = await repository.add_device(
            conn,
            user_id=user.user_id,
            token_id=user.token_id,
            kind=body.kind,
            address=address,
            params=params,
            label=body.label,
        )
        if added is None:
            raise HTTPException(409, f"you have {MAX_DEVICES} push devices already: remove one before adding another")
        row, new = added
        if new:
            await _record(conn, user, audit.PUSH_DEVICE_ADD, row.id, body.kind)
    log.info("push device added", extra={"login": user.login, "kind": body.kind, "new": new})
    return _device(row, user.token_id)


async def list_devices(engine, user: Principal) -> PushDeviceList:
    """``user``'s push devices that push reaches, newest first."""
    async with engine.begin() as conn:
        rows = await repository.devices_of(conn, user.user_id)
    return PushDeviceList(devices=[_device(row, user.token_id) for row in rows])


async def remove_device(engine, user: Principal, device_id: int) -> None:
    """Remove ``user``'s push device ``device_id``; 404 when they have none of that id."""
    async with engine.begin() as conn:
        removed = await repository.remove_device(conn, device_id, user.user_id)
        if removed is None:
            raise HTTPException(404, f"you have no push device {device_id}")
        await _record(conn, user, audit.PUSH_DEVICE_REMOVE, device_id, removed[1])
    log.info("push device removed", extra={"login": user.login, "kind": removed[1]})


def _sender(kind: str, config) -> PushSender:
    """An instance of the sender of devices of ``kind``; 503 when the hub sends to none of them now."""
    cls = channel_class(kind)
    if cls is None or not issubclass(cls, PushSender):
        raise HTTPException(503, f"this hub does not send to {kind} devices yet")
    missing = cls.missing(config)
    if missing:
        raise HTTPException(503, f"this hub does not send to {kind} devices: set {', '.join(missing)}")
    return cls(config)


async def try_device(engine, config, user: Principal, device_id: int) -> PushTestResult:
    """Push a test notification to ``user``'s device ``device_id`` now, on a hub configured with ``config``."""
    async with engine.begin() as conn:
        device = await repository.device_of(conn, user.user_id, device_id)
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
            if await repository.remove_device(conn, device.id, user.user_id) is not None:
                await _record(conn, user, audit.PUSH_DEVICE_REMOVE, device.id, device.kind)
        log.info("push device gone on a test", extra={"login": user.login, "kind": device.kind})
        return PushTestResult(sent=False, removed=True, error=_said(exc))
    except Exception as exc:  # whatever the push service or the network did is the answer, not a 500
        log.info("test push not sent", extra={"login": user.login, "kind": device.kind}, exc_info=True)
        return PushTestResult(sent=False, removed=False, error=_said(exc))
    async with engine.begin() as conn:
        await repository.sent(conn, device.id)
    return PushTestResult(sent=True, removed=False, error=None)
