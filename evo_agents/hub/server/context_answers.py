"""The confirmed answers in the prompt of a run (plan active-context-answers, criteria a1 to a4).

When the project's policy sets ``context.answers`` (false by default), the prompt of each run ends, before the index of
an agent's memories (``memory_upkeep.with_index``), with a block of what people already answered that matches the
work: the decisions of the project that were answered (the question, the option chosen, the answerer's words, who and
when) and its feedback memories of the project's scope. The block is built by rules, with no model:

- scope: an item cites code as ``code:REPO:PATH`` (a decision's evidence, question, context and answer; a memory's
  text). It matches a run by path (the run's prompt, which holds its plan step, names the path: 3), by directory (it
  names a directory of the path: 2) or by repo (the run works in the repo: 1). The project is always the run's own, the
  guest of the answer, and a decision of category architecture or scope adds 1 to a run that works on a plan. An item
  that matches by none of the three is not in the block, and the block is empty when none is;
- order: by match, then newest; the block stops at BLOCK_BYTES and at the room the prompt leaves under MAX_PROMPT_BYTES,
  and each line names its source, to open with ``memory_get`` or ``decision show``.

Never in the block: an item of another project; a decision of category credential, wait, release or loop (EXCLUDED); a
memory the run's owner could not open with memory_get through the agent's sink (``AGENT_SINK``): deleted, archived, an
agent's own, someone else's feedback, whose label the sink does not reach or whose audience the agent of the run does not
hold (``kg_audience.agent_audience``); a decision whose run showed its agent an audience the agent of this run does not
hold. The audiences it shows are noted to the run (``run_audience``). With the key off, or with no match, the prompt is
returned as it came, byte for byte.

``context_answer_loads`` records each answer loaded: the run, the source, its id and the scope key that matched
(``repo:REPO``, ``dir:REPO:DIR`` or ``path:REPO:PATH``). ``load_counts`` counts them a day or a week, and ``week_counts``
sets them against the runs queued (the daily signal and the weekly read: ``context_answer_reuse``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from posixpath import dirname

from sqlalchemy import BigInteger, bindparam, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.runs import MAX_PROMPT_BYTES, PLAN_KINDS
from evo_agents.hub.server import run_audience
from evo_agents.hub.server.autonomy_policies import effective_policy
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.kg_health import owner_access
from evo_agents.hub.server.mcp_text import memory_about
from evo_agents.hub.server.memory_audience import label_audience
from evo_agents.hub.server.memory_upkeep import citations
from evo_agents.kg.policy import audience_below

log = logging.getLogger(__name__)

BLOCK_BYTES = 4 * 1024  # the block in a prompt, at most
CANDIDATES = 200  # answers of each source read, newest first
EXCLUDED = frozenset({"credential", "wait", "release", "loop"})  # categories of decision that never load
SOURCES = ("decision", "memory")  # context_answer_loads.source
PATH, DIR, REPO = 3, 2, 1  # what a scope key weighs
CATEGORY_BONUS = frozenset({"architecture", "scope"})  # categories that add 1 to a run on a plan
QUESTION_CHARS, ANSWER_CHARS = 300, 500
BUCKETS = ("day", "week")

_RUN = (
    select(
        tables.runs.c.id,
        tables.runs.c.kind,
        tables.runs.c.project_id,
        tables.runs.c.repo,
        tables.runs.c.repos,
        tables.runs.c.dispatched_by.label("owner_id"),
        tables.projects.c.name.label("project"),
        tables.users.c.login,
    )
    .join(tables.projects, tables.projects.c.id == tables.runs.c.project_id)
    .join(tables.users, tables.users.c.id == tables.runs.c.dispatched_by)
    .where(tables.runs.c.id == bindparam("run_id", type_=BigInteger))
)  # run :run_id, its project and the person who dispatched it

_DECISIONS = (
    select(
        tables.decisions.c.id,
        tables.decisions.c.run_id,
        tables.decisions.c.category,
        tables.decisions.c.question,
        tables.decisions.c.context,
        tables.decisions.c.options,
        tables.decisions.c.answer_option,
        tables.decisions.c.answer_text,
        tables.decisions.c.answered_at,
        tables.decisions.c.evidence,
        tables.users.c.login.label("answerer"),
    )
    .join(tables.users, tables.users.c.id == tables.decisions.c.answered_by, isouter=True)
    .where(
        tables.decisions.c.project_id == bindparam("project_id", type_=BigInteger),
        tables.decisions.c.state == "answered",
        tables.decisions.c.answered_at.is_not(None),
        tables.decisions.c.category.not_in(sorted(EXCLUDED)),
    )
    .order_by(tables.decisions.c.answered_at.desc(), tables.decisions.c.id.desc())
    .limit(CANDIDATES)
)  # the answered decisions of project :project_id a block may carry, newest first

_FEEDBACK = (
    select(
        tables.memories.c.id,
        tables.memories.c.name,
        tables.memories.c.label,
        tables.memories.c.body,
        tables.memories.c.updated_at,
        tables.users.c.login.label("writer"),
    )
    .join(tables.users, tables.users.c.id == tables.memories.c.updated_by)
    .where(
        tables.memories.c.scope == "project",
        tables.memories.c.project_id == bindparam("project_id", type_=BigInteger),
        tables.memories.c.type == "feedback",
        tables.memories.c.owner_id == bindparam("owner_id", type_=BigInteger),  # memory_get opens feedback to its owner
        ~tables.memories.c.deleted,
        tables.memories.c.profile_id.is_(None),
        tables.memories.c.archived_at.is_(None),
        tables.memories.c.promoted_at.is_(None),
    )
    .order_by(tables.memories.c.updated_at.desc(), tables.memories.c.id.desc())
    .limit(CANDIDATES)
)  # the feedback memories of project :project_id that run's owner owns, newest first


@dataclass(frozen=True)
class Answer:
    """One answer that matches a run: its source and id, the key and weight of the match, when it was made, and the line
    it takes in the block."""

    source: str
    source_id: int
    scope_key: str
    weight: int
    at: datetime
    line: str
    audience: frozenset[str] = frozenset()
    run_id: int | None = None  # the run that asked a decision


def _flat(text: str | None, chars: int) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= chars else one[: chars - 3].rstrip() + "..."


def match(found: set[str], repos: set[str], text: str) -> tuple[int, str] | None:
    """The strongest way the cited ``REPO:PATH`` items ``found`` match a run that works in ``repos`` and whose prompt
    is ``text``: its weight and scope key; None when none does. An item of a repo the run does not work in (it is not in
    ``repos`` and the prompt does not name it) never matches."""
    best: tuple[int, str] | None = None
    for item in sorted(found):
        repo, _, path = item.partition(":")
        if not path or not (repo in repos or repo in text):
            continue
        if path in text:
            return PATH, f"path:{repo}:{path}"
        found_here = (REPO, f"repo:{repo}") if repo in repos else None
        directory = dirname(path)
        while directory:
            if f"{directory}/" in text:
                found_here = (DIR, f"dir:{repo}:{directory}")
                break
            directory = dirname(directory)
        if found_here is not None and (best is None or found_here[0] > best[0]):
            best = found_here
    return best


def _repos(run) -> set[str]:
    names = {run.repo} if run.repo else set()
    for entry in run.repos or []:
        if isinstance(entry, dict) and isinstance(entry.get("repo"), str):
            names.add(entry["repo"])
    return names


def _decision_line(row) -> str:
    chosen = next((o for o in row.options if o.get("key") == row.answer_option), None)
    option = (chosen or {}).get("label") or row.answer_option or "answered"
    words = f": {_flat(row.answer_text, ANSWER_CHARS)}" if row.answer_text else ""
    who = f" by {row.answerer}" if row.answerer else ""
    return (
        f"- decision {row.id} ({row.category}, answered {row.answered_at:%Y-%m-%d}{who}): "
        f"{_flat(row.question, QUESTION_CHARS)} -> {_flat(option, 120)}{words}. "
        f"Open: evo-agents hub decision show {row.id}"
    )


async def enabled(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the policy of project ``project_id`` sets ``context.answers`` now."""
    return (await effective_policy(conn, project_id))["context"]["answers"] is True


async def _decisions(conn, run, repos: set[str], text: str) -> list[Answer]:
    found = []
    for row in (await conn.execute(_DECISIONS, {"project_id": run.project_id})).all():
        refs = citations(" ".join([row.question, row.context or "", row.answer_text or "", *(row.evidence or [])]))
        hit = match(refs, repos, text)
        if hit is None:
            continue
        weight = hit[0] + (1 if row.category in CATEGORY_BONUS and run.kind in PLAN_KINDS else 0)
        found.append(
            Answer("decision", row.id, hit[1], weight, row.answered_at, _decision_line(row), run_id=row.run_id)
        )
    return found


async def _memories(conn, run, access, repos: set[str], text: str) -> list[Answer]:
    found = []
    for row in (await conn.execute(_FEEDBACK, {"project_id": run.project_id, "owner_id": run.owner_id})).all():
        if not access.visible(row.label, AGENT_SINK):
            continue
        hit = match(citations(row.body), repos, text)
        if hit is None:
            continue
        line = (
            f"- memory {row.id} ({row.name}, feedback, {row.updated_at:%Y-%m-%d} by {row.writer}): "
            f"{memory_about(row.body)} Open: memory_get {row.id}"
        )
        found.append(Answer("memory", row.id, hit[1], hit[0], row.updated_at, line, label_audience(row.label)))
    return found


async def block(conn: AsyncConnection, run_id: int, prompt: str) -> tuple[str, list[Answer]]:
    """The block of confirmed answers for run ``run_id`` whose prompt is ``prompt``, and the answers in it; empty when
    the key is off, the run's owner has no access, or no answer matches."""
    run = (await conn.execute(_RUN, {"run_id": run_id})).one_or_none()
    if run is None or not await enabled(conn, run.project_id):
        return "", []
    access = await owner_access(conn, run.owner_id, run.login, run.project)
    if access is None:
        return "", []
    repos = _repos(run)
    found = [
        *await _decisions(conn, run, repos, prompt),
        *await _memories(conn, run, access, repos, prompt),
    ]
    if not found:
        return "", []
    found.sort(key=lambda a: (-a.weight, -a.at.timestamp(), a.source, -a.source_id))
    head = [
        "# Confirmed answers",
        f"People already answered these in project {run.project}, for work like this run's, best match first. "
        "Rely on one only after opening its source.",
    ]
    room = min(BLOCK_BYTES, MAX_PROMPT_BYTES - len(prompt.rstrip("\n").encode()) - 2)
    held, lines, taken = None, [], []
    for answer in found:
        audience = answer.audience
        if answer.run_id is not None:  # what the run that asked it showed its agent
            audience = await run_audience.seen(conn, answer.run_id)
        if audience:
            held = await agent_audience(conn, run.owner_id, run_id, run.kind) if held is None else held
            if not audience_below(audience, held):
                continue
        line = answer.line
        if len("\n".join([*head, *lines, line]).encode()) + 1 > room:
            break
        lines.append(line)
        taken.append(answer)
    if not taken:
        return "", []
    await run_audience.note(conn, run_id, {p for a in taken for p in a.audience})
    return "\n".join([*head, *lines]) + "\n", taken


async def with_answers(conn: AsyncConnection, run_id: int, prompt: str) -> str:
    """``prompt``, the prompt of run ``run_id``, with the block of confirmed answers at its end when the policy sets
    ``context.answers`` and some answer matches (recorded in ``context_answer_loads``); ``prompt`` as it is otherwise."""
    text, taken = await block(conn, run_id, prompt)
    if not taken:
        return prompt
    project_id = (await conn.execute(select(tables.runs.c.project_id).where(tables.runs.c.id == run_id))).scalar_one()
    rows = [
        {
            "run_id": run_id,
            "project_id": project_id,
            "source": a.source,
            "source_id": a.source_id,
            "scope_key": a.scope_key,
        }
        for a in taken
    ]
    await conn.execute(insert(tables.context_answer_loads).values(rows).on_conflict_do_nothing())
    log.info("confirmed answers loaded", extra={"run_id": run_id, "answers": len(taken)})
    return prompt.rstrip("\n") + "\n\n" + text


@dataclass(frozen=True)
class LoadCount:
    """The answers loaded in one ``bucket`` (a day or a week, UTC) from ``start``: how many runs loaded some and how
    many answers they loaded."""

    start: datetime
    runs: int
    answers: int


async def load_counts(
    conn: AsyncConnection, project_id: int, since: datetime, until: datetime, bucket: str = "day"
) -> list[LoadCount]:
    """The loads of project ``project_id`` from ``since`` up to ``until``, by ``bucket`` ("day" or "week", UTC),
    oldest first; a bucket with no load is not listed."""
    if bucket not in BUCKETS:
        raise ValueError(f"bucket is one of {', '.join(BUCKETS)}, not {bucket!r}")
    c = tables.context_answer_loads
    start = func.date_trunc(bucket, func.timezone("UTC", c.c.loaded_at)).label("start")
    query = (
        select(start, func.count(func.distinct(c.c.run_id)).label("runs"), func.count().label("answers"))
        .where(c.c.project_id == project_id, c.c.loaded_at >= since, c.c.loaded_at < until)
        .group_by(start)
        .order_by(start)
    )
    return [LoadCount(r.start, r.runs, r.answers) for r in await conn.execute(query)]


@dataclass(frozen=True)
class WeekCount:
    """One week (UTC, from ``start``) of the runs queued in it: how many ran in all, how many loaded at least one
    answer, and how many answers they loaded."""

    start: datetime
    runs: int
    runs_with_loads: int
    answers: int


async def week_counts(conn: AsyncConnection, project_id: int, since: datetime, until: datetime) -> list[WeekCount]:
    """The runs of project ``project_id`` queued from ``since`` up to ``until``, by week (UTC), oldest first, with the
    answers those runs loaded; a week with no run is not listed. The loads count in the week their run was queued, so
    ``runs_with_loads`` never passes ``runs``."""
    r, c = tables.runs, tables.context_answer_loads
    week = func.date_trunc("week", func.timezone("UTC", r.c.queued_at)).label("start")  # a naive UTC time
    found = {
        row.start.replace(tzinfo=UTC): WeekCount(row.start.replace(tzinfo=UTC), row.runs, 0, 0)
        for row in await conn.execute(
            select(week, func.count().label("runs"))
            .where(r.c.project_id == project_id, r.c.queued_at >= since, r.c.queued_at < until)
            .group_by(week)
        )
    }
    loaded = await conn.execute(
        select(week, func.count(func.distinct(c.c.run_id)).label("runs"), func.count().label("answers"))
        .select_from(c.join(r, r.c.id == c.c.run_id))
        .where(c.c.project_id == project_id, r.c.queued_at >= since, r.c.queued_at < until)
        .group_by(week)
    )
    for row in loaded:
        start = row.start.replace(tzinfo=UTC)
        base = found.get(start) or WeekCount(start, row.runs, 0, 0)
        found[start] = WeekCount(start, max(base.runs, row.runs), row.runs, row.answers)
    return [found[start] for start in sorted(found)]
