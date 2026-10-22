"""The job ``daily`` of the plugin pm (criteria a6 and a8 of plan pm-daily-tracking): each working morning, the digest
of every project that enables pm, and the escalation of each blocker that has lasted too long, each sent once a day.

When. The manifest's cron is ``*/15 * * * *``, in UTC: every 15 minutes. For each project that enables pm the job takes
the local moment in the project's time zone (``timezone``); a day that is not one of its ``working_days`` (6 and 7
count when listed), or a local time before ``digest_at`` ("HH:MM", 08:00 by default), ends the pass with nothing done,
as does a project whose pm or tasks config cannot be used (logged). From ``digest_at`` on, the first pass of the day
sends and the later ones find ``pm_sends`` already claimed, so a day sends once however often the job runs.

What. The report of the day is computed as the route computes it (``report``). The digest goes to the PM (``pm``, else
the project's admins) as the notice ``pm_daily`` when the project grants the plugin ``notifications:send``, and into
the group chat ``digest_chat`` as a message of the hub when it grants ``chats:write`` and the chat is still there with
an agent of the project (otherwise it is logged and skipped). Each blocker in ``escalate`` goes as the notice
``pm_escalation`` to the PM and to whom it waits on: the member, the assignees of the task it waits on, or the owner of
the run whose decision is open; a blocker that waits on a text goes to the PM alone.

What it says. Counts, codes of tasks and the link to the project's pm page: never a title or a description, because
Telegram forwards a notice and a chat's label may be lower than a task's. A notice counts what its recipient may see
(the report computed with their grant, as the route does for them); the chat's message counts what the project's top
grant sees.

Once. ``pm_sends`` keeps one row per project, day and thing sent (the digest, a blocker's escalation). A row is
claimed before the thing is sent, in the transaction that sends it, so a job that runs again or twice the same day
sends nothing more, and one that fails sends nothing and claims nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.core_plugins.pm import reads, report, settings
from evo_agents.core_plugins.pm.facts import Config, Inputs
from evo_agents.hub import signals, tables
from evo_agents.hub.plugin_calls import CoreContext, effective_grant, enabled_in
from evo_agents.hub.server.chat_access import agent_projects
from evo_agents.hub.server.chat_runs import hub_reply
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.projects import ProjectAccess

log = logging.getLogger(__name__)

PLUGIN = settings.PLUGIN
JOB = "daily"
CRON = "*/15 * * * *"
DIGEST, ESCALATION = "digest", "escalation"  # evo_agents.hub.tables_pm.KINDS
NOTICE_DAILY, NOTICE_ESCALATION = "pm_daily", "pm_escalation"  # evo_agents.hub.runs.NOTICE_KINDS
SEND, WRITE = "notifications:send", "chats:write"
COUNTED = ("done_tasks", "done_use_cases", "late", "review", "stale", "blocked", "escalate")
CODED = ("late", "stale", "escalate")  # the sections whose task codes the message names


def link(project: str) -> str:
    """The pm page of ``project`` on the hub's web."""
    return f"/p/{quote(project, safe='')}/x/pm"


def counts(daily: dict) -> dict[str, int]:
    """How many entries each section of ``daily`` holds."""
    return {name: len(daily[name]) for name in COUNTED}


def codes(daily: dict) -> dict[str, list[str]]:
    """The task codes of the sections of ``daily`` that name them, each once, in order."""
    return {name: list(dict.fromkeys(entry["ref"] for entry in daily[name])) for name in CODED if daily[name]}


def digest_text(project: str, day: date, daily: dict) -> str:
    """The words of a digest: counts and task codes, no title."""
    found = counts(daily)
    lines = [
        f"Daily digest of {project} for {day.isoformat()}: done {found['done_tasks']} tasks and "
        f"{found['done_use_cases']} use cases; late {found['late']}, in review {found['review']}, "
        f"stale {found['stale']}, blocked {found['blocked']}, to escalate {found['escalate']}."
    ]
    for name, refs in codes(daily).items():
        lines.append(f"{name}: {', '.join(refs)}")
    lines.append(link(project))
    return "\n".join(lines)


async def _grants(conn: AsyncConnection, project_id: int) -> dict[str, tuple[int, str, str | None]]:
    """login -> (user id, role, max level) of each member with a grant on the project."""
    g, u = tables.grants, tables.users
    query = (
        select(u.c.login, u.c.id, g.c.role, g.c.max_level)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return {row.login: (row.id, row.role, row.max_level) for row in await conn.execute(query)}


def managers(grants: dict, config: Config, project: str) -> list[str]:
    """The logins the digest and the escalations are for: the config's pm, else the admins of the project."""
    if config.pm and config.pm in grants:
        return [config.pm]
    if config.pm:
        log.warning("pm names a member with no grant: the admins are told", extra={"project": project, "pm": config.pm})
    return [login for login, (_, role, _) in grants.items() if role == "admin"]


async def _claim(conn: AsyncConnection, project_id: int, day: date, kind: str, key: str = "") -> bool:
    """Whether this is the first time ``kind``/``key`` is sent for the project on ``day``; it is kept as sent."""
    sends = tables.pm_sends
    statement = (
        pg_insert(sends)
        .values(project_id=project_id, day=day, kind=kind, key=key)
        .on_conflict_do_nothing(constraint="pm_sends_once_key")
        .returning(sends.c.id)
    )
    return (await conn.execute(statement)).scalar() is not None


class Reach:
    """What each recipient of a project's job may see: their report's facts, read with their own grant, once."""

    def __init__(self, conn: AsyncConnection, project_id: int, rules, config: Config, day: date, grants: dict):
        self.conn, self.project_id, self.rules = conn, project_id, rules
        self.config, self.day, self.grants = config, day, grants
        self._seen: dict[str, Inputs] = {}

    async def of(self, login: str) -> Inputs:
        if login not in self._seen:
            _, role, max_level = self.grants[login]
            access = ProjectAccess(self.project_id, self.rules, role, max_level)
            self._seen[login] = await reads.load(self.conn, access, None, self.config, self.day)
        return self._seen[login]

    async def top(self) -> Inputs:
        """The facts as the project's top grant reads them: what a chat's message counts."""
        access = ProjectAccess(self.project_id, self.rules, "admin", self.rules.levels[-1])
        return await reads.load(self.conn, access, None, self.config, self.day)


async def _tell_digest(conn: AsyncConnection, project_id: int, project: str, reach: Reach, people: list[str]) -> int:
    told = 0
    for login in people:
        daily = report.daily(await reach.of(login), reach.day)
        await notify(
            conn,
            user_id=reach.grants[login][0],
            kind="notice",
            notice_kind=NOTICE_DAILY,
            title=f"Daily digest of {project} for {reach.day.isoformat()}",
            body=digest_text(project, reach.day, daily),
            project_id=project_id,
            details={"date": reach.day.isoformat(), "counts": counts(daily), "tasks": codes(daily)},
            link=link(project),
        )
        told += 1
    return told


async def _chat_ready(conn: AsyncConnection, chat_id: int, project_id: int) -> bool:
    """Whether chat ``chat_id`` is still there with an agent of the project."""
    if await conn.scalar(select(tables.chats.c.id).where(tables.chats.c.id == chat_id)) is None:
        return False
    return any(found.id == project_id for found in await agent_projects(conn, chat_id))


async def _post_digest(conn: AsyncConnection, project_id: int, project: str, reach: Reach) -> bool:
    chat_id = reach.config.digest_chat
    if chat_id is None:
        return False
    if not await _chat_ready(conn, chat_id, project_id):
        log.warning(
            "the digest chat is gone or has no agent of the project", extra={"project": project, "chat": chat_id}
        )
        return False
    daily = report.daily(await reach.top(), reach.day)
    await hub_reply(conn, chat_id, None, digest_text(project, reach.day, daily))
    return True


def _waiting(data: Inputs, blocker) -> list[str]:
    """The logins a blocker waits on: the member, or the assignees of the task; none for a text."""
    if blocker.waiting_kind == "member":
        return [blocker.waiting_on]
    if blocker.waiting_kind == "task":
        return [login for task in data.tasks if task.ref == blocker.waiting_on for login in task.assignees]
    return []


def blocker_key(blocker) -> str:
    return f"decision:{blocker.decision_id}" if blocker.decision_id is not None else f"blocker:{blocker.blocker_id}"


async def _escalate(
    conn: AsyncConnection, project_id: int, project: str, reach: Reach, managing: list[str]
) -> dict[str, int]:
    done = {"escalations": 0, "told": 0}
    top = await reach.top()
    for task, blocker, age in report.escalations(top, reach.day):
        if not await _claim(conn, project_id, reach.day, ESCALATION, blocker_key(blocker)):
            continue
        done["escalations"] += 1
        people = list(dict.fromkeys([*managing, *(w for w in _waiting(top, blocker) if w in reach.grants)]))
        for login in people:
            if not any(t.ref == task.ref and t.visible for t in (await reach.of(login)).tasks):
                continue
            await notify(
                conn,
                user_id=reach.grants[login][0],
                kind="notice",
                notice_kind=NOTICE_ESCALATION,
                title=f"{task.ref} has been blocked for {age} working days in {project}",
                project_id=project_id,
                details={"task": task.ref, "kind": blocker.kind, "age_days": age, "date": reach.day.isoformat()},
                link=link(project),
            )
            done["told"] += 1
    return done


async def run_project(engine: AsyncEngine, project: str, *, now: datetime | None = None) -> dict:
    """One pass of the job over ``project``; what it did, counted, or why it did nothing."""
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(tables.projects.c.id).where(tables.projects.c.name == project))
        row = await enabled_in(conn, PLUGIN, project_id) if project_id is not None else None
        if row is None:
            return {}
        try:
            config = await settings.config(conn, project_id, project)
            await settings.task_groups(conn, project_id, project)
        except settings.PmError as exc:
            log.warning("pm cannot run for the project", extra={"project": project, "why": exc.message})
            return {"skipped": exc.message}
        local = (now or datetime.now(UTC)).astimezone(config.calendar.zone)
        day = local.date()
        if not config.calendar.working(day):
            return {"skipped": "not a working day", "date": day.isoformat()}
        if local.time().replace(tzinfo=None) < config.digest_at:
            return {"skipped": "before digest_at", "date": day.isoformat()}
        granted = effective_grant(row)
        sending, posting = SEND in granted, WRITE in granted
        grants = await _grants(conn, project_id)
        reach = Reach(conn, project_id, await signals.project_rules(conn, project_id), config, day, grants)
        managing = managers(grants, config, project)
        done: dict = {"date": day.isoformat()}
        if (sending or posting) and await _claim(conn, project_id, day, DIGEST):
            done["digest"] = {"told": 0, "posted": False}
            if sending:
                done["digest"]["told"] = await _tell_digest(conn, project_id, project, reach, managing)
            if posting:
                done["digest"]["posted"] = await _post_digest(conn, project_id, project, reach)
        if sending:
            done.update(await _escalate(conn, project_id, project, reach, managing))
    log.info("pm daily", extra={"project": project, **done})
    return done


async def run(params: dict, context: CoreContext) -> dict:
    """The job ``daily``: one pass over each project ``params`` names (``projects``)."""
    found = {}
    for project in params.get("projects") or []:
        try:
            found[project] = await run_project(context.engine, project)
        except Exception:  # one project's failure leaves the others their digest
            log.exception("pm daily failed", extra={"project": project})
            found[project] = {"failed": True}
    return {"projects": found}


__all__ = ["CRON", "JOB", "blocker_key", "counts", "digest_text", "link", "run", "run_project"]
