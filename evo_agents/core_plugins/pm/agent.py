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
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.pm import service, settings
from evo_agents.core_plugins.pm.settings import PmError
from evo_agents.hub.server import core_plugin_api as hub


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def _person(conn: AsyncConnection, params: dict, context: hub.Context) -> hub.Session:
    """The member the tool acts for; PmError (403) for the agent of a run that acts for nobody."""
    found = await hub.tool_caller(conn, settings.handle(), context, params)
    if found.session is None and found.run_id is not None and found.run_kind != "chat":
        raise PmError(
            403,
            f"the agent of a {found.run_kind} run reads no daily report: only a chat run does, for the member who mentioned it",
        )
    if found.session is None:
        raise PmError(403, "the caller is no member of the hub")
    return found.session


async def _access(conn: AsyncConnection, person: hub.Session, project: str) -> hub.ProjectRef:
    try:
        return await hub.project_access(conn, person, project)
    except HTTPException:
        raise PmError(403, f"{person.login} holds no grant on project {project} now") from None


def _day(args: dict) -> date | None:
    try:
        return date.fromisoformat(args["date"]) if args.get("date") else None
    except ValueError:
        raise PmError(422, f"date {args['date']!r} is no calendar day: write it YYYY-MM-DD") from None


async def _run(params: dict, context: hub.Context, which: str) -> str:
    args = params.get("arguments") or {}
    day = _day(args)
    async with hub.transaction(context) as conn:
        access = await _access(conn, await _person(conn, params, context), context.project)
        if which == "daily":
            answer = await service.daily(conn, access, context.sink, day)
        else:
            answer = await service.member(conn, access, context.sink, day, args["login"])
    return json.dumps(answer.model_dump(mode="json"), ensure_ascii=False)


async def _answer(params: dict, context: hub.Context, which: str) -> dict:
    try:
        return _text(await _run(params, context, which))
    except PmError as exc:
        return _text(f"error: {exc.message}", error=True)
    except HTTPException as exc:
        return _text(f"error: {exc.detail}", error=True)


async def daily(params: dict, context: hub.Context) -> dict:
    """The tool ``pm__daily``: the daily report of the project for the caller."""
    return await _answer(params, context, "daily")


async def member(params: dict, context: hub.Context) -> dict:
    """The tool ``pm__member``: the view of one member in the daily report of the project."""
    return await _answer(params, context, "member")


__all__ = ["daily", "member"]
