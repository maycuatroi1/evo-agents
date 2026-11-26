"""The routes of push notifications: a member's push devices, a test push to one, and what a client needs to subscribe.
Each passes the request's parts to ``service.devices``; ``docs/notifications.md`` is the design.

POST /v1/me/push/devices adds a device of the caller's, bound to the token they called with: a browser's Web Push
subscription as ``PushSubscription.toJSON()`` gives it (its endpoint must be a push service's, its keys a P-256 key
and a 16-byte secret), an APNs token with its topic and environment, or an FCM token. Adding a device the caller has
refreshes it; one another member had moves to the caller. A member has at most ``models.MAX_DEVICES`` (409).
Audited as push_device.add, naming the device's id and kind, never its endpoint or token. GET /v1/me/push/devices
lists the caller's devices that reach them (kind, label, when added, when a push last reached it, and whether this
sign-in added it). DELETE /v1/me/push/devices/{id} removes one (push_device.remove). POST
/v1/me/push/devices/{id}/test pushes a test notification to it now and says whether it went; a device its push service
no longer knows is removed, and a kind whose sender is off answers 503.

GET /v1/push/config, without a sign-in, gives the public VAPID key browsers subscribe with (null while Web Push is off)
and which senders are on.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.push.models import (
    DeviceId,
    DeviceIn,
    PushConfig,
    PushDevice,
    PushDeviceList,
    PushTestResult,
)
from evo_agents.hub.server.push.service import devices
from evo_agents.hub.server.security import CurrentUser

config_router = APIRouter(prefix="/v1/push", tags=["push"])
router = APIRouter(prefix="/v1/me/push", tags=["push"], responses={401: {"model": ErrorBody}})


@config_router.get("/config", response_model=PushConfig)
async def push_config(request: Request) -> PushConfig:
    """The public VAPID key and the senders that are on; no sign-in needed."""
    return devices.push_config(request.app.state.config)


@router.post(
    "/devices",
    status_code=201,
    response_model=PushDevice,
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def add_device(request: Request, body: Annotated[DeviceIn, Body()], user: CurrentUser) -> PushDevice:
    """Add a push device of the caller's, or refresh it."""
    return await devices.add_device(request.app.state.engine, user, body)


@router.get("/devices", response_model=PushDeviceList)
async def list_devices(request: Request, user: CurrentUser) -> PushDeviceList:
    """The caller's push devices that push reaches, newest first."""
    return await devices.list_devices(request.app.state.engine, user)


@router.delete("/devices/{device_id}", status_code=204, responses={404: {"model": ErrorBody}})
async def remove_device(request: Request, device_id: DeviceId, user: CurrentUser) -> Response:
    """Remove a push device of the caller's; the pushes waiting for it go with it."""
    await devices.remove_device(request.app.state.engine, user, device_id)
    return Response(status_code=204)


@router.post(
    "/devices/{device_id}/test",
    response_model=PushTestResult,
    responses={404: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def try_device(request: Request, device_id: DeviceId, user: CurrentUser) -> PushTestResult:
    """Push a test notification to one of the caller's devices now."""
    return await devices.try_device(request.app.state.engine, request.app.state.config, user, device_id)
