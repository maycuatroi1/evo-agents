"""The notice of the files a run's agent made for its owner (``runs.OUTPUT_NOTICE``, plan run-outputs), what of a
notification's files a channel may send, and the notices a member turned off. ``docs/notifications.md`` (Notices,
What never goes to Telegram) is the design.

``outputs_recorded`` runs in the transaction that records outputs (``store.record``), holding the run's row.
The files of sources sent and outputs it took (an image taken out of a tool's result never) become one notice of kind
``run_outputs`` to the run's owner, gathered a minute a run: the first files store the notification with its
deliveries due OUTPUT_NOTICE_SECONDS later, and the files that come before any channel tried it join it (its details,
title and body). The notice only reports (``runs.REPORT_NOTICE_KINDS``): it is stored read and goes to the web
alone, so the gathering holds its web delivery and no channel or device ever gets it. Once the web took it, or is
taking it now (its delivery locked), the next files start a new notice, due a minute after they came. A chat
run sends none: its files go under its messages in the thread. A file a question carries goes with the question:
``withdraw`` takes it out of the run's notice no channel tried yet, and drops a notice left with none. A member who
turned the kind off (``muted``) gets no such notice; their decisions and every other notice still come.

``channel_outputs`` is what a channel cleared for a notification's project may send of its files (``member_channels
.prepare``): those of a decision, or of a notice of a run's files, never of source tool, at most CHANNEL_OUTPUTS of
them, each with a presigned GET of its bytes when the job's pass has the blob store (``notifications.PASS_BLOBS``).

GET and PUT /v1/me/notification-settings read and set the notices the caller turned off, whether their push
notifications hide details (``hide_details``: only the kind of thing, the project and the link, ``push_payload``), the
tier of each kind of notification (interrupt, brief or off, and whether the caller chose it or it is the hub's
default), their quiet hours, their time zone (an IANA name Postgres knows, as the Curator's charter is checked; the
hub's EVO_HUB_WEB_TIME_ZONE until they choose one) and the local time of their summary push (``push_tiers``); audited
``notification.settings``, naming the kinds and the choices. A PUT sets what it names and leaves the rest: a tier, the
quiet hours, the time zone or the time of the summary push set to null goes back to the hub's default (no quiet hours).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, runs
from evo_agents.hub.config import DEFAULT_WEB_TIME_ZONE
from evo_agents.hub.server import audit, push_tiers
from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.notifications import PASS_BLOBS, Outgoing, notify, one_line, run_link
from evo_agents.hub.server.outputs.models.notices import (
    NoticeSetting,
    NotificationSettings,
    NotificationSettingsIn,
    QuietHours,
    TierSetting,
)
from evo_agents.hub.server.outputs.repository import notices as repository
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_push import DEFAULT_DIGEST_AT

log = logging.getLogger(__name__)

GATHER = timedelta(seconds=runs.OUTPUT_NOTICE_SECONDS)
NAMES_IN_BODY = 20  # files a notice's body names; the rest it counts
CAPTION_CHARS = 200  # of a file's caption in a notice's body
CHANNEL_OUTPUTS = 10  # files a channel gets of one notification: a media group of Telegram holds 10


# The notices a member turned off


async def muted(conn: AsyncConnection, user_id: int, notice_kind: str) -> bool:
    """Whether member ``user_id`` turned the notices of ``notice_kind`` off."""
    return await repository.muted(conn, user_id, notice_kind)


async def _settings(conn: AsyncConnection, user_id: int, hub_zone: str) -> NotificationSettings:
    off = await repository.muted_kinds(conn, user_id)
    row = await push_tiers.preferences(conn, user_id)
    chosen = push_tiers.chosen_tiers(row)
    tiers = [
        TierSetting(kind=kind, tier=chosen.get(kind, push_tiers.kind_default(kind)), chosen=kind in chosen)
        for kind in push_tiers.KINDS
    ]
    quiet = None
    if row is not None and row.quiet_start is not None and row.quiet_end is not None:
        quiet = QuietHours(start=row.quiet_start, end=row.quiet_end)
    zone = row.time_zone if row is not None else None
    return NotificationSettings(
        notices=[NoticeSetting(kind=k, on=k not in off) for k in runs.MUTABLE_NOTICE_KINDS],
        hide_details=bool(row is not None and row.hide_details),
        tiers=tiers,
        quiet_hours=quiet,
        time_zone=zone or hub_zone,
        time_zone_chosen=zone is not None,
        digest_at=(row.digest_at if row is not None else None) or DEFAULT_DIGEST_AT,
    )


def _hub_zone(request: Request) -> str:
    return getattr(request.app.state.config, "web_time_zone", None) or DEFAULT_WEB_TIME_ZONE


async def settings(request: Request, user: Principal) -> NotificationSettings:
    """The notification settings of the caller (see the module)."""
    async with UnitOfWork(request.app.state.engine) as conn:
        return await _settings(conn, user.user_id, _hub_zone(request))


async def set_settings(request: Request, user: Principal, body: NotificationSettingsIn) -> NotificationSettings:
    """Set what ``body`` names of the caller's notification settings, audited; the settings as they are then."""
    off = [setting.kind for setting in body.notices if not setting.on]
    on = [setting.kind for setting in body.notices if setting.on]
    async with UnitOfWork(request.app.state.engine) as conn:
        if body.time_zone is not None and not await push_tiers.time_zone_known(conn, body.time_zone):
            raise Unprocessable(
                f"{body.time_zone!r} is not a time zone the hub knows: give an IANA name such as Asia/Tokyo"
            )
        if on:
            await repository.unmute(conn, user.user_id, on)
        if off:
            await repository.mute(conn, user.user_id, off)
        await push_tiers.set_preferences(conn, user.user_id, body.preferences())
        shown = body.shown()
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.NOTIFICATION_SETTINGS,
            target=f"notifications:settings {shown}",
        )
        found = await _settings(conn, user.user_id, _hub_zone(request))
    log.info("notification settings set", extra={"login": user.login, "settings": shown})
    return found


# The notice of a run's files


def _size(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def _who(kind: str, run_id: int) -> str:
    return f"Run #{run_id}" if kind == "plan" else f"{kind.capitalize()} run #{run_id}"


def _text(kind: str, run_id: int, project: str, files: list) -> tuple[str, str]:
    """(the title, the body) of a notice of ``files`` (rows of id, name, size, caption) of a run."""
    title = f"{_who(kind, run_id)} sent " + (files[0].name if len(files) == 1 else f"{len(files)} files")
    lines = []
    for row in files[:NAMES_IN_BODY]:
        line = f"- {one_line(row.name)} ({_size(row.size)})"
        if row.caption:
            line += f": {one_line(row.caption, CAPTION_CHARS)}"
        lines.append(line)
    if len(files) > NAMES_IN_BODY:
        lines.append(f"- and {len(files) - NAMES_IN_BODY} more")
    return title, "\n".join([*lines, "", f"Files of {_who(kind, run_id).lower()} in project {project}."])


def _ids(details) -> list[int]:
    found = details.get("outputs") if isinstance(details, dict) else None
    return [value for value in found or [] if isinstance(value, int)]


async def _rewrite(conn: AsyncConnection, notification_id: int, run, files: list) -> None:
    title, body = _text(run.kind, run.id, run.project, files)
    await repository.rewrite(
        conn,
        notification_id,
        one_line(title),
        runs.clip(body, runs.MAX_NOTICE_BODY_BYTES),
        {"outputs": [f.id for f in files]},
    )


async def _told(conn: AsyncConnection, run) -> set[int]:
    """The outputs a notice of ``run``'s files to its owner names already: a file sent again with a caption, say, is
    not told twice."""
    return {
        output_id
        for details in await repository.told_details(conn, run.id, run.dispatched_by)
        for output_id in _ids(details)
    }


async def outputs_recorded(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> int | None:
    """Tell the owner of run ``run_id``, whose row the caller holds, of its outputs ``output_ids`` just recorded or
    changed (see the module); the notification's id, or None when nothing is sent."""
    run = await repository.run(conn, run_id)
    if run is None or run.kind == "chat" or await muted(conn, run.dispatched_by, runs.OUTPUT_NOTICE):
        return None
    files = await repository.files(conn, run_id, [i for i in output_ids if i not in await _told(conn, run)])
    if not files:
        return None
    found = await repository.untried_of(conn, run_id, run.dispatched_by)
    if found is not None and await repository.held(conn, found.id):  # else the job's pass is sending it: a new notice
        await _rewrite(conn, found.id, run, await repository.files(conn, run_id, [*_ids(found.details), *output_ids]))
        return found.id
    title, body = _text(run.kind, run_id, run.project, files)
    notification_id = await notify(
        conn,
        user_id=run.dispatched_by,
        kind="notice",
        notice_kind=runs.OUTPUT_NOTICE,
        project_id=run.project_id,
        run_id=run_id,
        title=title,
        body=body,
        details={"outputs": [row.id for row in files]},
        link=run_link(run.project, run_id),
    )
    await repository.gather(conn, notification_id, GATHER)
    return notification_id


async def withdraw(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> None:
    """Take outputs ``output_ids``, which a question of run ``run_id`` carries now, out of the run's notices of files
    that no channel has tried yet; a notice left with none goes, its deliveries with it."""
    if not output_ids:
        return
    run = await repository.run(conn, run_id)
    if run is None:
        return
    for found in await repository.untried(conn, run_id):
        held = _ids(found.details)
        if not set(held) & set(output_ids) or not await repository.held(conn, found.id):
            continue
        files = await repository.files(conn, run_id, held)
        if files:
            await _rewrite(conn, found.id, run, files)
        else:
            await repository.drop(conn, found.id)


# What a channel may send of a notification's files


async def _channel_files(conn: AsyncConnection, notification: Outgoing) -> list | None:
    """The files of ``notification`` a channel may send, oldest first, or None when it carries none."""
    if notification.kind == "decision" and notification.decision_id is not None:
        return await repository.decision_files(conn, notification.decision_id)
    if notification.notice_kind == runs.OUTPUT_NOTICE and notification.run_id is not None:
        ids = _ids(notification.details)
        return await repository.run_files(conn, notification.run_id, ids) if ids else None
    return None


async def _link(sha256: str, name: str) -> str | None:
    store = PASS_BLOBS.get()
    if store is None:
        return None
    return await asyncio.to_thread(store.presign_get, sha256, filename=outputs.download_name(name))


async def channel_outputs(conn: AsyncConnection, notification: Outgoing) -> tuple[list[dict], int] | None:
    """(the files of ``notification`` a channel may send, at most CHANNEL_OUTPUTS oldest first, and how many there
    are) for a decision or a notice of a run's files, or None for any other notification. A file is its id, name,
    media type, viewer, size, caption and ``url``: a presigned GET of its bytes for GET_TTL, None when the job's pass
    has no blob store. No image taken out of a tool's result is among them."""
    rows = await _channel_files(conn, notification)
    if rows is None:
        return None
    files = [
        {
            "id": row.id,
            "name": row.name,
            "media_type": row.media_type,
            "viewer": outputs.viewer(row.media_type),
            "size": row.size,
            "caption": row.caption,
            "url": await _link(row.sha256, row.name),
        }
        for row in rows[:CHANNEL_OUTPUTS]
    ]
    return files, len(rows)
