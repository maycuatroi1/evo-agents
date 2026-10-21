"""The tools of the plugin pm on the hub's /mcp: ``pm__daily`` and ``pm__member``, the data of the route
``GET /v1/x/pm/<project>/daily`` as JSON text for the caller.

The hub lists them in a session whose project enabled the plugin and checks their arguments against the manifest's
schema first. They act for a person: the agent of a chat run for the person who mentioned the agent, a member's own
session for that member. The person must hold a grant on the project; they see the tasks the routes of tasks show them,
through the session's sink, and what is hidden adds to the counts only. Nothing is written.
"""

from __future__ import annotations

import json
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.pm import service
from evo_agents.core_plugins.pm.settings import PmError
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def _person(conn: AsyncConnection, params: dict, context: CoreContext) -> Principal:
    """The member the tool acts for; PmError (403) for the agent of a run that acts for nobody."""
    u = tables.users
    if context.run_id is None:
        login = (params.get("caller") or {}).get("login") or ""
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == login))).scalar_one_or_none()
    else:
        found = await chat_run(conn, context.run_id)
        if found is None:
            kind = (
                await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == context.run_id))
            ).scalar_one()
            raise PmError(
                403,
                f"the agent of a {kind} run reads no daily report: only a chat run does, for the member who mentioned it",
            )
        user_id = found.dispatcher_id
    if user_id is None:
        raise PmError(403, "the caller is no member of the hub")
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    admin = context.config is not None and context.config.is_admin(login)
    return Principal(user_id, login, admin, None, MACHINE, "")


async def _access(conn: AsyncConnection, person: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, person, project)
    except HTTPException:
        raise PmError(403, f"{person.login} holds no grant on project {project} now") from None


def _day(args: dict) -> date | None:
    try:
        return date.fromisoformat(args["date"]) if args.get("date") else None
    except ValueError:
        raise PmError(422, f"date {args['date']!r} is no calendar day: write it YYYY-MM-DD") from None


async def _run(params: dict, context: CoreContext, which: str) -> str:
    args = params.get("arguments") or {}
    day = _day(args)
    async with context.engine.begin() as conn:
        access = await _access(conn, await _person(conn, params, context), context.project)
        if which == "daily":
            answer = await service.daily(conn, access, context.sink, day)
        else:
            answer = await service.member(conn, access, context.sink, day, args["login"])
    return json.dumps(answer.model_dump(mode="json"), ensure_ascii=False)


async def _answer(params: dict, context: CoreContext, which: str) -> dict:
    try:
        return _text(await _run(params, context, which))
    except PmError as exc:
        return _text(f"error: {exc.message}", error=True)
    except HTTPException as exc:
        return _text(f"error: {exc.detail}", error=True)


async def daily(params: dict, context: CoreContext) -> dict:
    """The tool ``pm__daily``: the daily report of the project for the caller."""
    return await _answer(params, context, "daily")


async def member(params: dict, context: CoreContext) -> dict:
    """The tool ``pm__member``: the view of one member in the daily report of the project."""
    return await _answer(params, context, "member")


__all__ = ["daily", "member"]
