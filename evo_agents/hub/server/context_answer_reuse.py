"""How often the confirmed answers are loaded (plan active-context-answers, criteria a3 and a6).

Daily, ``hub.context_answer_reuse`` (``push``) posts, for each project whose policy sets ``context.answers``, the signal
``context.answer_reuse``: the answers loaded into prompts on the day before (UTC), measured at the start of that day with
the external id ``context.answer_reuse:YYYY-MM-DD``, so a rerun posts nothing twice. A project with the key off gets
nothing. The signal goes through the project's ingest key ``context-answers`` (made on first use, owned by its first
admin) and the key must be in the project's registry: where it is not, the hub refuses the signal, logs it and the table
``context_answer_loads`` keeps the figures.

``GET /v1/projects/{project}/context-answers`` (``evo-agents hub context answers``) gives the weeks: the runs queued, the
runs that loaded at least one answer and the answers loaded, to set the weeks after the policy revision that turned the
key on against those before. Any member with a grant on the project reads it.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import signals, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import context_answers
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import principal
from evo_agents.hub.server.signals import SignalIn

log = logging.getLogger(__name__)

SIGNAL_KEY = "context.answer_reuse"
SIGNAL_SOURCE = "context-answers"
INGEST_KEY = "context-answers"  # the project's ingest key the signal comes through, made on first use
DEFAULT_WEEKS = 8
MAX_WEEKS = 52

router = APIRouter(prefix="/v1/projects", tags=["context"], responses={401: {"model": ErrorBody}})


class ContextAnswerWeek(BaseModel):
    start: AwareDatetime
    runs: int
    runs_with_loads: int
    answers: int


class ContextAnswerWeeks(BaseModel):
    project: str
    enabled: bool
    since: AwareDatetime
    until: AwareDatetime
    weeks: list[ContextAnswerWeek]


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


@router.get(
    "/{project}/context-answers",
    response_model=ContextAnswerWeeks,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def read_weeks(
    request: Request,
    project: Annotated[str, PROJECT_NAME],
    weeks: Annotated[
        int, Query(ge=1, le=MAX_WEEKS, description="how many weeks, the current one included")
    ] = DEFAULT_WEEKS,
) -> ContextAnswerWeeks:
    """By week (UTC): the runs of the project, those that loaded at least one confirmed answer, and the answers loaded."""
    user = principal(request)
    now = datetime.now(UTC)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "reader"):
            raise HTTPException(403, f"reading the answers loaded in project {project} needs a grant on it")
        rows = await weekly(conn, access.project_id, now, weeks)
        on = await context_answers.enabled(conn, access.project_id)
    return ContextAnswerWeeks(project=project, enabled=on, since=rows[0].start, until=now, weeks=rows)


async def _pusher(conn: AsyncConnection, project_id: int) -> signals.Pusher | None:
    """The pusher of the project's ingest key INGEST_KEY, made on first use and owned by the project's first admin;
    None when the project has no admin or revoked the key."""
    k, g = tables.signal_ingest_keys, tables.grants
    owner = await conn.scalar(
        select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
    )
    if owner is None:
        return None
    rules = await signals.project_rules(conn, project_id)
    found = (
        await conn.execute(
            select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project_id, k.c.name == INGEST_KEY)
        )
    ).all()
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        return None
    if live is None:
        live = (
            await conn.execute(
                insert(k)
                .values(
                    project_id=project_id,
                    name=INGEST_KEY,
                    max_level=rules.default_label.get("level"),
                    token_hash=signals.hash_token(signals.new_ingest_token()),  # nobody reads it: the hub writes itself
                    created_by=owner,
                )
                .returning(k.c.id, k.c.max_level)
            )
        ).one()
    return signals.Pusher(project_id, rules, "ingest", live.id, INGEST_KEY, live.max_level)


async def push(engine: AsyncEngine, config: HubConfig, now: datetime | None = None) -> dict:
    """Post ``context.answer_reuse`` for the day before ``now`` to each project whose policy sets ``context.answers``;
    what was done. A project that fails does not stop the others."""
    today: date = (now or datetime.now(UTC)).astimezone(UTC).date()
    start = datetime.combine(today - timedelta(days=1), time.min, UTC)
    done = {"day": start.date().isoformat(), "projects": 0, "pushed": 0, "duplicates": 0, "refused": [], "failed": []}
    async with engine.connect() as conn:
        projects = (await conn.execute(select(tables.projects.c.id, tables.projects.c.name))).all()
    for project_id, name in projects:
        try:
            async with engine.begin() as conn:
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
