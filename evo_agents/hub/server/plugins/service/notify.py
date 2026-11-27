"""A notice from an isolated plugin to the members of a project (``routes.notify`` has the design): who gets it, its
link, and the key that makes a call once."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, runs
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.plugins.models.notify import (
    CONTROL,
    NOTICE,
    PERMISSION,
    ROLE_WORDS,
    PluginNoticeIn,
    PluginNoticeSent,
)
from evo_agents.hub.server.plugins.repository import notify as repository
from evo_agents.hub.server.plugins.repository.enabled import enabled_in

log = logging.getLogger(__name__)


def check_body(body: PluginNoticeIn) -> None:
    if body.body is not None and len(body.body.encode()) > runs.MAX_NOTICE_BODY_BYTES:
        raise HTTPException(422, f"the body is longer than {runs.MAX_NOTICE_BODY_BYTES} bytes in UTF-8")


async def _enabled(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, granted notifications:send;
    403 otherwise, the same for a project that does not exist."""
    enabled = await enabled_in(conn, plugin_id, project)
    if enabled is None or PERMISSION not in plugin_calls.effective_grant(enabled):
        raise HTTPException(
            403,
            f"the plugin is not enabled in project {project} with {PERMISSION} granted: an admin of the project "
            "enables it and grants what it asks for",
        )
    return enabled


def _link(body: PluginNoticeIn, enabled) -> str:
    """The link of the notice: ``body.link`` when it stays in the plugin's pages or the project's page; else 422."""
    home = f"/p/{enabled.project}"
    page = f"{home}/x/{enabled.name}/"
    if body.link is None:
        return page if (enabled.manifest.get("contributes") or {}).get("pages") else home
    link = body.link
    path = link.split("?", 1)[0].split("#", 1)[0]
    inside = path == page.rstrip("/") or path.startswith(page)
    if CONTROL.search(link) or "\\" in link or ".." in path.split("/") or not (inside or path == home):
        raise HTTPException(422, f"the link is a path under {page}, or {home}")
    return link


async def _recipients(conn: AsyncConnection, project_id: int, to: list[str]) -> tuple[list, list[str]]:
    """(the members of ``to`` with a grant on the project, the logins of ``to`` without one)."""
    held = await repository.members(conn, project_id)
    wanted = {item.lower() for item in to if item not in ROLE_WORDS}
    words = [ROLE_WORDS[item] for item in to if item in ROLE_WORDS]
    chosen = [row for row in held if row.login.lower() in wanted or any(has_role(row.role, needed) for needed in words)]
    skipped = [
        item for item in to if item not in ROLE_WORDS and item.lower() not in {row.login.lower() for row in held}
    ]
    return chosen, skipped


async def send(conn: AsyncConnection, plugin_id: int, project: str, body: PluginNoticeIn) -> PluginNoticeSent:
    """Send the notice ``body`` to members of ``project`` as plugin ``plugin_id``; for a key sent already, the first
    answer, ``duplicate`` true."""
    enabled = await _enabled(conn, plugin_id, project)
    link = _link(body, enabled)
    if body.key is not None:
        # one send of a key at a time: a second call waits here for the first to commit, then finds its row
        if not await repository.claim_key(conn, enabled.project_id, plugin_id, body.key):
            first = await repository.first_result(conn, enabled.project_id, plugin_id, body.key)
            return PluginNoticeSent(**first, duplicate=True)  # pyright: ignore[reportCallIssue]
    chosen, skipped = await _recipients(conn, enabled.project_id, body.to)
    details = {"plugin": enabled.name, "version": enabled.installed_version, "topic": body.topic, "key": body.key}
    for row in chosen:
        await NOTIFIER.notify(
            conn,
            user_id=row.id,
            kind="notice",
            notice_kind=NOTICE,
            title=body.title,
            body=body.body,
            project_id=enabled.project_id,
            details=details,
            link=link,
        )
    result = {"sent": len(chosen), "recipients": [row.login for row in chosen], "skipped": skipped}
    if body.key is not None:
        await repository.keep_result(conn, enabled.project_id, plugin_id, body.key, result)
    await audit.record(
        conn,
        actor_id=None,
        token_id=None,
        action=audit.PLUGIN_NOTIFY,
        target=f"{enabled.project}/plugin:{enabled.name} recipients:{len(chosen)}",
        project_id=enabled.project_id,
    )
    log.info("plugin notice sent", extra={"project": project, "plugin": enabled.name, "recipients": len(chosen)})
    return PluginNoticeSent(**result, duplicate=False)
