"""The senders of push devices: the classes ``notifications.CHANNELS`` names for the kinds of push_devices, which the
job hub.deliver_notifications hands each delivery to a device, as it hands a channel's to the channel's class.

A sender's ``prepare`` reads what a push may carry of a notification (``push_payload``): whether its project is kept
from Telegram by the hub sink's clearance (``member_channels``, with the clearance Telegram has), whether its member
chose to hide details (``push_store.hides_details``), and for a decision that is neither, its question and options.
``send`` builds the payload and hands it to ``deliver`` with the device's address and params as ``config``; the route
that sends a test push calls ``deliver`` itself. A device the push service no longer knows raises ChannelGone, which
removes the device; a sender whose configuration is missing raises Undeliverable, so the delivery fails at once and
the rest of the outbox goes on.

WebPushChannel sends to a browser's subscription: the payload encrypted for it (RFC 8291), the hub's VAPID JWT
(RFC 8292), a TTL of TTL_SECONDS and urgency high, to the subscription's endpoint, which must be a push service's
(``webpush.endpoint_problem``). The push service's 404 or 410 says the subscription is gone; its 429 asks to wait;
another 4xx fails the delivery; anything else is tried again.
"""

from __future__ import annotations

import dataclasses
import email.utils
import time
from typing import ClassVar

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import push_payload, tables, telegram
from evo_agents.hub.config import VAPID_VARIABLES
from evo_agents.hub.server import member_channels, push_store, webpush
from evo_agents.hub.server.notifications import (
    BACKOFF_SECONDS,
    Channel,
    ChannelGone,
    Outgoing,
    RetryAfter,
    Undeliverable,
    channel_class,
)
from evo_agents.hub.tables_push import KINDS, WEBPUSH

TTL_SECONDS = 24 * 3600  # how long a push service keeps a push for a device that is offline
HTTP_TIMEOUT_SECONDS = 20.0
MAX_REASON_CHARS = 200  # of a push service's answer, in a delivery's last_error


def device_config(address: str, params: dict | None) -> dict:
    """What a sender's ``deliver`` takes of a device: its address and its params."""
    return {**(params or {}), "address": address}


class PushSender(Channel):
    """Sends pushes to the devices of one kind (``kind``); off while ``missing`` names a variable."""

    kind: ClassVar[str] = ""

    @classmethod
    def missing(cls, config) -> list[str]:
        """The variables this sender still needs on a hub configured with ``config``; empty when it is on."""
        return []

    async def prepare(self, conn: AsyncConnection, notification: Outgoing) -> Outgoing:
        if notification.project is None:
            restricted = notification.kind in member_channels.ANSWERABLE
        else:
            rules = await member_channels.rules_of(conn, notification.project)
            restricted = member_channels.kept_from(
                rules, telegram.RESTRICTED_LEVEL, telegram.RESTRICTED_LOCATION, below=True
            )
        extra: dict = {
            "restricted": restricted,
            "hide_details": await push_store.hides_details(conn, notification.user_id),
        }
        if not restricted and not extra["hide_details"] and notification.decision_id is not None:
            d = tables.decisions
            asked = select(d.c.question, d.c.options).where(d.c.id == notification.decision_id)
            row = (await conn.execute(asked)).one_or_none()
            extra["decision"] = None if row is None else {"question": row.question, "options": row.options}
        return dataclasses.replace(notification, extra=extra)

    def public_url(self) -> str | None:
        return getattr(self.config, "public_url", None)

    async def send(self, notification: Outgoing, config: dict) -> str | None:
        return await self.deliver(push_payload.payload(notification, self.public_url()), config)

    async def deliver(self, payload: dict, config: dict) -> str | None:
        """Push ``payload`` to the device ``config`` describes (``device_config``); the id the service gave it."""
        raise NotImplementedError


def _retry_after(answer: httpx.Response) -> float:
    """The seconds a 429 asks to wait: its Retry-After in seconds or as a date, else BACKOFF_SECONDS."""
    said = answer.headers.get("retry-after", "").strip()
    if said.isdigit():
        return float(said)
    try:
        return max(1.0, email.utils.parsedate_to_datetime(said).timestamp() - time.time())
    except (TypeError, ValueError):
        return float(BACKOFF_SECONDS)


def _reason(answer: httpx.Response) -> str:
    text = " ".join(answer.text.split())[:MAX_REASON_CHARS]
    return f"the push service answered {answer.status_code}" + (f": {text}" if text else "")


def outcome(answer: httpx.Response) -> str | None:
    """The id a push service's answer gives a push it took (its Location), or the exception its refusal is."""
    status = answer.status_code
    if 200 <= status < 300:
        return answer.headers.get("location")
    if status in (404, 410):
        raise ChannelGone(f"{_reason(answer)}; the device's subscription is gone")
    if status == 429:
        raise RetryAfter(_retry_after(answer), _reason(answer))
    if 400 <= status < 500:
        raise Undeliverable(_reason(answer))
    raise RuntimeError(_reason(answer))


class WebPushChannel(PushSender):
    """Web Push to a browser's subscription (the module)."""

    kind = WEBPUSH
    transport: ClassVar[httpx.AsyncBaseTransport | None] = None  # tests hand the push service in here

    @classmethod
    def missing(cls, config) -> list[str]:
        return list(VAPID_VARIABLES) if config is None else config.webpush_missing()

    def application_server_key(self) -> str | None:
        """The public VAPID key browsers subscribe with, or None while Web Push is off."""
        return None if self.missing(self.config) else webpush.public_key(self.config.vapid_private_key)

    async def deliver(self, payload: dict, config: dict) -> str | None:
        missing = self.missing(self.config)
        if missing:
            raise Undeliverable(f"Web Push is off on this hub: set {', '.join(missing)}")
        endpoint = config.get("address")
        problem = webpush.endpoint_problem(endpoint)
        if problem is not None:
            raise Undeliverable(problem)
        try:
            body = webpush.encrypt(push_payload.encoded(payload), config.get("p256dh"), config.get("auth"))
        except ValueError as exc:
            raise Undeliverable(f"the device's subscription keys do not work: {exc}") from None
        headers = {
            "Authorization": webpush.vapid_authorization(
                endpoint, self.config.vapid_private_key, self.config.push_subject
            ),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL_SECONDS),
            "Urgency": "high",
        }
        async with httpx.AsyncClient(transport=self.transport, timeout=HTTP_TIMEOUT_SECONDS) as http:
            answer = await http.post(endpoint, content=body, headers=headers)
        return outcome(answer)


def senders_on(config) -> dict[str, bool]:
    """Each kind of push device, and whether the hub configured with ``config`` sends to it now."""
    found = {}
    for kind in KINDS:
        cls = channel_class(kind)
        found[kind] = cls is not None and issubclass(cls, PushSender) and not cls.missing(config)
    return found
