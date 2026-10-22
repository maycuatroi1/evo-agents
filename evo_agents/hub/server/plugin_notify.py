"""A notice from an isolated plugin to the members of a project (``docs/plugins.md``, section Permissions).

POST /v1/plugin-api/projects/{p}/notifications takes the token of a plugin's backend (``Authorization: Bearer evp_...``)
and no other credential, as the rest of /v1/plugin-api/ (``task_dispatch``). No token, a malformed one, or one that is
revoked or whose plugin is not installed or is turned off gets 401; a project that did not enable the plugin, or
whose grant lacks ``notifications:send`` among what the installed version still asks for, gets 403 (checked at each
call); a body that is wrong, or a ``link`` outside the plugin's pages and the project, gets 422.

``to`` names logins and/or the roles ``admins`` (the project's admins), ``writers`` (its writers and admins) and
``members`` (everyone who holds a grant). Each of them with a grant on the project gets one notification of kind notice,
notice_kind ``plugin_notice``, whose details name the plugin, its version, the ``topic`` and the ``key``; the
notifications go to the outbox in the transaction of the call (``notifications.notify``), so the web's Inbox and each
channel of the member receive them as any notice. A login without a grant is dropped and named in the answer. The
route has no rate limit.

A call with a ``key`` is made once: the first stores its answer in plugin_notice_keys with the notifications, and a
later call of the same plugin in the same project with the same key creates nothing and answers the first one's result.
The audit row ``plugin.notify`` names the plugin, the project and the number of recipients, never the title or body.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import TITLE_CHARS, notify
from evo_agents.hub.server.task_dispatch import plugin_of_request

log = logging.getLogger(__name__)

PERMISSION = "notifications:send"
NOTICE = "plugin_notice"  # evo_agents.hub.runs.NOTICE_KINDS
ROLE_WORDS = {"admins": "admin", "writers": "writer", "members": "reader"}  # the lowest role each word reaches
MAX_TO = 100
MAX_KEY_CHARS = 200
TOPIC = r"^[a-z0-9][a-z0-9._-]{0,63}$"
LOGIN = re.compile(LOGIN_NAME)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
REFUSALS = {code: {"model": ErrorBody} for code in (401, 403, 422)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


class PluginNoticeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[str] = Field(
        min_length=1,
        max_length=MAX_TO,
        description="logins and/or the roles admins, writers (writers and admins) and members (everyone with a grant)",
    )
    title: str = Field(min_length=1, max_length=TITLE_CHARS, description="one line")
    body: str | None = Field(None, description=f"at most {runs.MAX_NOTICE_BODY_BYTES} bytes in UTF-8")
    link: str | None = Field(
        None,
        max_length=500,
        description="a path under /p/{project}/x/{plugin}/, or /p/{project}; default: the plugin's page, else the project",
    )
    topic: str = Field(pattern=TOPIC, description="a short slug the plugin chooses")
    key: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_KEY_CHARS,
        description="a second call of the plugin in the project with the same key creates nothing",
    )

    @field_validator("to")
    @classmethod
    def _to(cls, items: list[str]) -> list[str]:
        for item in items:
            if item not in ROLE_WORDS and not LOGIN.fullmatch(item):
                raise ValueError(f"{item!r} is neither a login nor one of {', '.join(ROLE_WORDS)}")
        return list(dict.fromkeys(items))

    @field_validator("title")
    @classmethod
    def _title(cls, title: str) -> str:
        title = title.strip()
        if not title or CONTROL.search(title):
            raise ValueError("the title is one line of text")
        return title

    @field_validator("key")
    @classmethod
    def _key(cls, key: str | None) -> str | None:
        if key is not None and CONTROL.search(key):
            raise ValueError("the key has no control characters")
        return key


class PluginNoticeSent(BaseModel):
    sent: int = Field(description="the members who got a notice: the number of recipients")
    recipients: list[str] = Field(description="their logins")
    skipped: list[str] = Field(description="the logins in `to` that hold no grant on the project, who got none")
    duplicate: bool = Field(
        description="the key was used already: nothing new was created and this is the first answer"
    )


async def _enabled(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, granted notifications:send;
    403 otherwise, the same for a project that does not exist."""
    query = plugin_calls.enabled_query().where(tables.plugins.c.id == plugin_id, tables.projects.c.name == project)
    enabled = (await conn.execute(query)).first()
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
    g, u = tables.grants, tables.users
    query = (
        select(u.c.id, u.c.login, g.c.role)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    held = list(await conn.execute(query))
    wanted = {item.lower() for item in to if item not in ROLE_WORDS}
    words = [ROLE_WORDS[item] for item in to if item in ROLE_WORDS]
    chosen = [row for row in held if row.login.lower() in wanted or any(has_role(row.role, needed) for needed in words)]
    skipped = [
        item for item in to if item not in ROLE_WORDS and item.lower() not in {row.login.lower() for row in held}
    ]
    return chosen, skipped


async def _first(conn: AsyncConnection, project_id: int, plugin_id: int, key: str) -> dict | None:
    k = tables.plugin_notice_keys
    query = select(k.c.result).where(k.c.project_id == project_id, k.c.plugin_id == plugin_id, k.c.key == key)
    return (await conn.execute(query)).scalar_one_or_none()


@router.post("/projects/{project}/notifications", status_code=201, response_model=PluginNoticeSent, responses=REFUSALS)
async def send_notice(
    request: Request, response: Response, project: ProjectName, body: PluginNoticeIn
) -> PluginNoticeSent:
    """Send a notice to members of the project, as the plugin whose token the request carries."""
    if body.body is not None and len(body.body.encode()) > runs.MAX_NOTICE_BODY_BYTES:
        raise HTTPException(422, f"the body is longer than {runs.MAX_NOTICE_BODY_BYTES} bytes in UTF-8")
    async with request.app.state.engine.begin() as conn:
        plugin_id = await plugin_of_request(request, conn)
        enabled = await _enabled(conn, plugin_id, project)
        link = _link(body, enabled)
        if body.key is not None:
            # one send of a key at a time: a second call waits here for the first to commit, then finds its row
            k = tables.plugin_notice_keys
            claim = pg_insert(k).values(project_id=enabled.project_id, plugin_id=plugin_id, key=body.key, result={})
            claimed = await conn.execute(claim.on_conflict_do_nothing().returning(k.c.key))
            if claimed.first() is None:
                response.status_code = 200
                return PluginNoticeSent(**await _first(conn, enabled.project_id, plugin_id, body.key), duplicate=True)
        chosen, skipped = await _recipients(conn, enabled.project_id, body.to)
        details = {"plugin": enabled.name, "version": enabled.installed_version, "topic": body.topic, "key": body.key}
        for row in chosen:
            await notify(
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
            k = tables.plugin_notice_keys
            await conn.execute(
                k.update()
                .where(k.c.project_id == enabled.project_id, k.c.plugin_id == plugin_id, k.c.key == body.key)
                .values(result=result)
            )
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
