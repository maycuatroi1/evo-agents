"""How often the confirmed answers are loaded (``evo_agents.hub.server.context_answer_reuse`` has the route and the
plan): the weeks a member reads, and the daily signal ``context.answer_reuse`` of each project with them on."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import signals
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import context_answers
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.answer_reuse import (
    INGEST_KEY,
    SIGNAL_KEY,
    SIGNAL_SOURCE,
    ContextAnswerWeek,
    ContextAnswerWeeks,
)
from evo_agents.hub.server.kg.repository import answer_reuse as repository
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.signals.models import SignalIn

log = logging.getLogger("evo_agents.hub.server.context_answer_reuse")  # the name its log lines had before the layers


def week_start(moment: datetime) -> datetime:
    """The Monday 00:00 UTC of the week of ``moment``."""
    day = moment.astimezone(UTC).date()
    return datetime.combine(day - timedelta(days=day.weekday()), time.min, UTC)


async def weekly(conn: AsyncConnection, project_id: int, now: datetime, weeks: int) -> list[ContextAnswerWeek]:
    """The last ``weeks`` weeks up to ``now``, the current one included, each listed, with zeros where nothing ran."""
    first = week_start(now) - timedelta(weeks=weeks - 1)
    found = {w.start: w for w in await context_answers.week_counts(conn, project_id, first, now)}
    rows = []
    for index in range(weeks):
        start = first + timedelta(weeks=index)
        held = found.get(start)
        rows.append(
            ContextAnswerWeek(
                start=start,
                runs=held.runs if held else 0,
                runs_with_loads=held.runs_with_loads if held else 0,
                answers=held.answers if held else 0,
            )
        )
    return rows


async def read_weeks(engine: AsyncEngine, user: Principal, project: str, weeks: int) -> ContextAnswerWeeks:
    """By week (UTC): the runs of the project, those that loaded at least one confirmed answer, and the answers loaded."""
    now = datetime.now(UTC)
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "reader"):
            raise HTTPException(403, f"reading the answers loaded in project {project} needs a grant on it")
        rows = await weekly(conn, access.project_id, now, weeks)
        on = await context_answers.enabled(conn, access.project_id)
    return ContextAnswerWeeks(project=project, enabled=on, since=rows[0].start, until=now, weeks=rows)


async def _pusher(conn: AsyncConnection, project_id: int) -> signals.Pusher | None:
    """The pusher of the project's ingest key INGEST_KEY, made on first use and owned by the project's first admin;
    None when the project has no admin or revoked the key."""
    owner = await repository.first_admin(conn, project_id)
    if owner is None:
        return None
    rules = await signals.project_rules(conn, project_id)
    found = await repository.ingest_keys(conn, project_id)
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        return None
    if live is None:
        live = await repository.add_ingest_key(conn, project_id, rules.default_label.get("level"), owner)
    return signals.Pusher(project_id, rules, "ingest", live.id, INGEST_KEY, live.max_level)


async def push(engine: AsyncEngine, config: HubConfig, now: datetime | None = None) -> dict:
    """Post ``context.answer_reuse`` for the day before ``now`` to each project whose policy sets ``context.answers``;
    what was done. A project that fails does not stop the others."""
    today: date = (now or datetime.now(UTC)).astimezone(UTC).date()
    start = datetime.combine(today - timedelta(days=1), time.min, UTC)
    done = {"day": start.date().isoformat(), "projects": 0, "pushed": 0, "duplicates": 0, "refused": [], "failed": []}
    async with engine.connect() as conn:
        projects = await repository.projects(conn)
    for project_id, name in projects:
        try:
            async with UnitOfWork(engine) as conn:
                if not await context_answers.enabled(conn, project_id):
                    continue
                done["projects"] += 1
                pusher = await _pusher(conn, project_id)
                if pusher is None:
                    done["refused"].append(f"{name}: no admin, or the key {INGEST_KEY} is revoked")
                    continue
                counted = await context_answers.load_counts(conn, project_id, start, start + timedelta(days=1))
                item = SignalIn(
                    key=SIGNAL_KEY,
                    value=sum(c.answers for c in counted),
                    source=SIGNAL_SOURCE,
                    measured_at=start,
                    external_id=f"{SIGNAL_KEY}:{start.date().isoformat()}",
                )
                pushed = await signals.push(conn, pusher, [item], config.signal_days)
            done["pushed"] += pushed.accepted
            done["duplicates"] += pushed.duplicates
            for refusal in pushed.rejected:
                done["refused"].append(f"{name}: {refusal['reason']}")
                log.warning("context.answer_reuse refused", extra={"project": name, "reason": refusal["reason"]})
        except Exception:
            log.warning("context.answer_reuse could not be posted", extra={"project": name}, exc_info=True)
            done["failed"].append(name)
    return done
