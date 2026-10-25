"""The notice of the files a run's agent made for its owner (``runs.OUTPUT_NOTICE``, plan run-outputs), what of a
notification's files a channel may send, and the notices a member turned off. ``docs/notifications.md`` (Notices,
What never goes to Telegram) is the design.

``outputs_recorded`` runs in the transaction that records outputs (``output_store.record``), holding the run's row.
The files of sources sent and outputs it took (an image taken out of a tool's result never) become one notice of kind
``run_outputs`` to the run's owner, gathered a minute a run: the first files store the notification with its
deliveries due OUTPUT_NOTICE_SECONDS later, and the files that come before any channel tried it join it (its details,
title and body, unread again). Once a channel took it, or is sending it now (its delivery locked), the next files
start a new notice, due a minute after they came, so no channel gets two notices of one run within a minute. A chat
run sends none: its files go under its messages in the thread. A file a question carries goes with the question:
``withdraw`` takes it out of the run's notice no channel tried yet, and drops a notice left with none. A member who
turned the kind off (``muted``) gets no such notice; their decisions and every other notice still come.

``channel_outputs`` is what a channel cleared for a notification's project may send of its files (``member_channels
.prepare``): those of a decision, or of a notice of a run's files, never of source tool, at most CHANNEL_OUTPUTS of
them, each with a presigned GET of its bytes when the job's pass has the blob store (``notifications.PASS_BLOBS``).

GET and PUT /v1/me/notification-settings read and set the notices the caller turned off, and whether their push
notifications hide details (``hide_details``: only the kind of thing, the project and the link, ``push_payload``);
audited ``notification.settings``, naming the kinds and the choice. A PUT sets what it names and leaves the rest.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server import audit, push_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import PASS_BLOBS, Outgoing, notify, one_line, run_link
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

GATHER = timedelta(seconds=runs.OUTPUT_NOTICE_SECONDS)
NAMES_IN_BODY = 20  # files a notice's body names; the rest it counts
CAPTION_CHARS = 200  # of a file's caption in a notice's body
CHANNEL_OUTPUTS = 10  # files a channel gets of one notification: a media group of Telegram holds 10

router = APIRouter(prefix="/v1/me", tags=["notifications"], responses={401: {"model": ErrorBody}})


# The notices a member turned off


async def muted(conn: AsyncConnection, user_id: int, notice_kind: str) -> bool:
    """Whether member ``user_id`` turned the notices of ``notice_kind`` off."""
    m = tables.notification_mutes
    found = select(m.c.user_id).where(m.c.user_id == user_id, m.c.notice_kind == notice_kind)
    return (await conn.execute(found)).first() is not None


class NoticeSetting(BaseModel):
    kind: Literal[runs.MUTABLE_NOTICE_KINDS] = Field(description="a kind of notice a member may turn off")
    on: bool = Field(description="false: the member gets no notice of this kind, on any channel")


HIDE_DETAILS = (
    "push notifications carry only the kind of thing, the project and the link, as on a locked screen anyone may read"
)


class NotificationSettings(BaseModel):
    notices: list[NoticeSetting] = Field(description="each kind of notice a member may turn off, and whether it is on")
    hide_details: bool = Field(False, description=HIDE_DETAILS)


class NotificationSettingsIn(BaseModel):
    notices: list[NoticeSetting] = Field(
        default_factory=list,
        max_length=len(runs.MUTABLE_NOTICE_KINDS),
        description="the kinds to set; the others stay",
    )
    hide_details: bool | None = Field(None, description=f"{HIDE_DETAILS}; unchanged when left out")

    @field_validator("notices")
    @classmethod
    def _once(cls, value: list[NoticeSetting]) -> list[NoticeSetting]:
        if len({setting.kind for setting in value}) != len(value):
            raise ValueError("name each kind of notice once")
        return value

    @model_validator(mode="after")
    def _something(self):
        if not self.notices and self.hide_details is None:
            raise ValueError("set notices, hide_details or both")
        return self


async def _settings(conn: AsyncConnection, user_id: int) -> NotificationSettings:
    m = tables.notification_mutes
    off = set((await conn.execute(select(m.c.notice_kind).where(m.c.user_id == user_id))).scalars())
    return NotificationSettings(
        notices=[NoticeSetting(kind=k, on=k not in off) for k in runs.MUTABLE_NOTICE_KINDS],
        hide_details=await push_store.hides_details(conn, user_id),
    )


@router.get("/notification-settings", response_model=NotificationSettings)
async def show_notification_settings(request: Request, user: CurrentUser) -> NotificationSettings:
    """The kinds of notice the caller may turn off, and whether each is on."""
    async with request.app.state.engine.begin() as conn:
        return await _settings(conn, user.user_id)


@router.put("/notification-settings", response_model=NotificationSettings, responses={422: {"model": ErrorBody}})
async def set_notification_settings(
    request: Request, body: NotificationSettingsIn, user: CurrentUser
) -> NotificationSettings:
    """Turn kinds of notice on or off for the caller, and choose whether push hides details; decisions and the other
    notices always come."""
    m = tables.notification_mutes
    off = [setting.kind for setting in body.notices if not setting.on]
    on = [setting.kind for setting in body.notices if setting.on]
    async with request.app.state.engine.begin() as conn:
        if on:
            await conn.execute(delete(m).where(m.c.user_id == user.user_id, m.c.notice_kind.in_(on)))
        if off:
            rows = [{"user_id": user.user_id, "notice_kind": kind} for kind in off]
            await conn.execute(pg_insert(m).values(rows).on_conflict_do_nothing())
        if body.hide_details is not None:
            await push_store.set_hide_details(conn, user.user_id, body.hide_details)
        chosen = [f"{setting.kind}={'on' if setting.on else 'off'}" for setting in body.notices]
        if body.hide_details is not None:
            chosen.append(f"hide_details={'on' if body.hide_details else 'off'}")
        shown = " ".join(chosen)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.NOTIFICATION_SETTINGS,
            target=f"notifications:settings {shown}",
        )
        settings = await _settings(conn, user.user_id)
    log.info("notification settings set", extra={"login": user.login, "settings": shown})
    return settings


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


def _carried_by_question():
    o, k = tables.run_outputs, tables.run_output_links
    return exists().where(k.c.output_id == o.c.id, k.c.decision_id.is_not(None))


async def _files(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> list:
    """The outputs of ``output_ids`` of run ``run_id`` a notice names: not of source tool, carried by no question,
    oldest first."""
    if not output_ids:
        return []
    o = tables.run_outputs
    query = select(o.c.id, o.c.name, o.c.size, o.c.caption).where(
        o.c.run_id == run_id, one_of(o.c.id, output_ids), o.c.source != outputs.TOOL, ~_carried_by_question()
    )
    return list((await conn.execute(query.order_by(o.c.id))).all())


def _untried(run_id: int):
    """The notices of run ``run_id``'s files that no channel has tried yet, newest first, locked."""
    n, dl = tables.notifications, tables.notification_deliveries
    tried = exists().where(dl.c.notification_id == n.c.id, or_(dl.c.state != "pending", dl.c.attempts > 0))
    return (
        select(n.c.id, n.c.user_id, n.c.details)
        .where(n.c.run_id == run_id, n.c.kind == "notice", n.c.notice_kind == runs.OUTPUT_NOTICE, ~tried)
        .order_by(n.c.id.desc())
        .with_for_update(of=n)
    )


async def _held(conn: AsyncConnection, notification_id: int) -> bool:
    """Whether every delivery of notification ``notification_id`` is pending, untried and locked now: false while the
    job's pass holds one (it is being sent)."""
    dl = tables.notification_deliveries
    of = dl.c.notification_id == notification_id
    total = (await conn.execute(select(func.count()).select_from(dl).where(of))).scalar_one()
    free = select(dl.c.id).where(of, dl.c.state == "pending", dl.c.attempts == 0).with_for_update(skip_locked=True)
    return len((await conn.execute(free)).all()) == total


def _ids(details) -> list[int]:
    found = details.get("outputs") if isinstance(details, dict) else None
    return [value for value in found or [] if isinstance(value, int)]


async def _rewrite(conn: AsyncConnection, notification_id: int, run, files: list) -> None:
    title, body = _text(run.kind, run.id, run.project, files)
    n = tables.notifications
    changed = update(n).values(
        title=one_line(title),
        body=runs.clip(body, runs.MAX_NOTICE_BODY_BYTES),
        details={"outputs": [f.id for f in files]},
    )
    await conn.execute(changed.values(read_at=None).where(n.c.id == notification_id))


async def _run(conn: AsyncConnection, run_id: int):
    r, p = tables.runs, tables.projects
    query = select(r.c.id, r.c.kind, r.c.project_id, r.c.dispatched_by, p.c.name.label("project")).join_from(
        r, p, p.c.id == r.c.project_id
    )
    return (await conn.execute(query.where(r.c.id == run_id))).one_or_none()


async def _told(conn: AsyncConnection, run) -> set[int]:
    """The outputs a notice of ``run``'s files to its owner names already: a file sent again with a caption, say, is
    not told twice."""
    n = tables.notifications
    query = select(n.c.details).where(
        n.c.run_id == run.id, n.c.user_id == run.dispatched_by, n.c.notice_kind == runs.OUTPUT_NOTICE
    )
    return {output_id for details in (await conn.execute(query)).scalars() for output_id in _ids(details)}


async def outputs_recorded(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> int | None:
    """Tell the owner of run ``run_id``, whose row the caller holds, of its outputs ``output_ids`` just recorded or
    changed (see the module); the notification's id, or None when nothing is sent."""
    run = await _run(conn, run_id)
    if run is None or run.kind == "chat" or await muted(conn, run.dispatched_by, runs.OUTPUT_NOTICE):
        return None
    files = await _files(conn, run_id, [i for i in output_ids if i not in await _told(conn, run)])
    if not files:
        return None
    mine = _untried(run_id).where(tables.notifications.c.user_id == run.dispatched_by).limit(1)
    found = (await conn.execute(mine)).one_or_none()
    if found is not None and await _held(conn, found.id):  # else the job's pass is sending it: a new notice
        await _rewrite(conn, found.id, run, await _files(conn, run_id, [*_ids(found.details), *output_ids]))
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
    dl = tables.notification_deliveries
    await conn.execute(update(dl).values(next_at=func.now() + GATHER).where(dl.c.notification_id == notification_id))
    return notification_id


async def withdraw(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> None:
    """Take outputs ``output_ids``, which a question of run ``run_id`` carries now, out of the run's notices of files
    that no channel has tried yet; a notice left with none goes, its deliveries with it."""
    if not output_ids:
        return
    run = await _run(conn, run_id)
    if run is None:
        return
    for found in (await conn.execute(_untried(run_id))).all():
        held = _ids(found.details)
        if not set(held) & set(output_ids) or not await _held(conn, found.id):
            continue
        files = await _files(conn, run_id, held)
        if files:
            await _rewrite(conn, found.id, run, files)
        else:
            await conn.execute(delete(tables.notifications).where(tables.notifications.c.id == found.id))


# What a channel may send of a notification's files


def _channel_files(notification: Outgoing):
    """The query of the files of ``notification`` a channel may send, or None when it carries none."""
    o, k = tables.run_outputs, tables.run_output_links
    shown = select(o.c.id, o.c.name, o.c.media_type, o.c.size, o.c.sha256, o.c.caption).where(
        o.c.source != outputs.TOOL
    )
    if notification.kind == "decision" and notification.decision_id is not None:
        carried = exists().where(k.c.output_id == o.c.id, k.c.decision_id == notification.decision_id)
        return shown.where(carried)
    if notification.notice_kind == runs.OUTPUT_NOTICE and notification.run_id is not None:
        ids = _ids(notification.details)
        return shown.where(o.c.run_id == notification.run_id, one_of(o.c.id, ids)) if ids else None
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
    query = _channel_files(notification)
    if query is None:
        return None
    rows = (await conn.execute(query.order_by(tables.run_outputs.c.id))).all()
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


__all__ = [
    "CHANNEL_OUTPUTS",
    "NotificationSettings",
    "NotificationSettingsIn",
    "channel_outputs",
    "muted",
    "outputs_recorded",
    "router",
    "withdraw",
]
