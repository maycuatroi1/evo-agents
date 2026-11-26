"""The channels a notification goes out on, a Strategy: Channel is what every class of channels does, WebChannel the
web's, which sends nothing, and CHANNELS names the class of each kind the hub has itself, a class or the
``module:name`` of one, imported when a pass first needs it (as the senders of push devices are,
``evo_agents.hub.server.push.service.senders``). A kind CHANNELS does not name takes the class a core plugin
contributes to the channels under that name (``channel_class``), as the core plugin telegram does.

While a class's ``prepare`` runs, SENDING holds the delivery it prepares for, and PASS_BLOBS the blob store of the
job's pass, so a channel's prepare can hand out presigned GETs of a notification's files."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.plugin_calls import core_registry, load
from evo_agents.hub.server.notifications.models import MAX_ERROR_CHARS, WEB, Outgoing, Sending
from evo_agents.hub.tables_push import APNS, FCM, WEBPUSH

log = logging.getLogger(__name__)

# The blob store of the job's pass, so a channel's prepare can hand out presigned GETs of a notification's files
# (``output_notices.channel_outputs``); None outside a pass, or on a hub without one.
PASS_BLOBS: ContextVar[BlobStore | None] = ContextVar("notification_pass_blobs", default=None)

# The delivery of the job's pass whose class is preparing now; None outside a pass's prepare.
SENDING: ContextVar[Sending | None] = ContextVar("notification_sending", default=None)


class Channel:
    """Sends notifications to the channels of one kind. A job's pass makes one instance of each class it needs, with
    the hub's config (None in tests); ``prepare`` may read more of a notification in the delivery's transaction, and
    ``send`` delivers it to one channel, whose ``config`` is the channel's own (empty for the web), answers the id the
    service gave the message (or None), and raises when it could not, so the delivery is tried again later."""

    needs_session = False  # a channel of this class lives no longer than the web session that linked it

    def __init__(self, config=None):
        self.config = config

    async def prepare(self, conn: AsyncConnection, notification: Outgoing) -> Outgoing:
        return notification

    async def send(self, notification: Outgoing, config: dict) -> str | None:
        raise NotImplementedError


class WebChannel(Channel):
    """The web reads notifications from the table: there is nothing to send."""

    async def send(self, notification: Outgoing, config: dict) -> str | None:
        return None


# The classes of the channels the hub has itself: a class, or the ``module:name`` of one, imported when needed.
CHANNELS: dict[str, type[Channel] | str] = {
    WEB: WebChannel,
    WEBPUSH: "evo_agents.hub.server.push.service.senders:WebPushChannel",  # push_devices of kind webpush
    APNS: "evo_agents.hub.server.push.service.native:ApnsChannel",  # ... of kind apns
    FCM: "evo_agents.hub.server.push.service.native:FcmChannel",  # ... of kind fcm
}


def error_text(exc: BaseException) -> str:
    """``exc`` as one line of at most MAX_ERROR_CHARS, its class first: a delivery's ``last_error``."""
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:MAX_ERROR_CHARS]


def loaded_class(kind: str) -> tuple[type[Channel] | None, str]:
    """(the class of channels of ``kind``, or None) and, for None, why: no class, or one that did not load."""
    if kind in CHANNELS:
        named = CHANNELS[kind]
        if not isinstance(named, str):
            return named, ""
        try:
            return cast("type[Channel]", load(named)), ""
        except Exception as exc:  # a class that does not import fails its own kind's deliveries, never the job
            log.warning("notification channel not loaded", extra={"channel": kind}, exc_info=True)
            return None, f"the class of channels of kind {kind} ({named}) did not load: {error_text(exc)}"
    contributed = core_registry().contributions("channels")
    found = next(
        ((manifest, item) for manifest, item in contributed if item["name"] == kind and "python" in item), None
    )
    if found is None:
        return None, f"the hub has no class for channels of kind {kind}"
    manifest, item = found
    try:
        return cast("type[Channel]", load(item["python"])), ""
    except Exception as exc:  # a core plugin's broken class fails its own kind's deliveries, never the job
        log.warning("core plugin channel not loaded", extra={"contribution": f"{manifest.id}:{kind}"}, exc_info=True)
        return None, f"the class of channels of kind {kind} (plugin {manifest.id}) did not load: {error_text(exc)}"


def channel_class(kind: str) -> type[Channel] | None:
    """The class CHANNELS names for ``kind``, else the one a core plugin contributes to the channels under that name
    (``evo_agents.hub.plugin_calls``), as the core plugin telegram does for kind telegram; None when there is none,
    or when the plugin's class does not load (logged)."""
    return loaded_class(kind)[0]
