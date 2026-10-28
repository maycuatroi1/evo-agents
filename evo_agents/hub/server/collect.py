"""The night's figures of a project and its review run: the job ``curator.collect`` and what ``fire_schedules`` asks
of it. ``evo_agents.hub.review`` holds the model (lenses, causes, the prompt), ``evo_agents.hub.server.curator`` the
night shift whose schedule queues the run, and ``evo_agents.hub.server.proposals`` what the run writes.

``collect`` is the job ``curator.collect``, every minute: it first measures the knowledge graph of each project with a
night shift whose last measure is old enough (``kg_health.measure``); then for each night_shift schedule that is not
paused, inside its charter's window, under the schedule's row lock (a schedule another job holds is passed over), it
counts the night's figures once (``ensure_figures``), as the owner of the schedule may read the project, and keeps them
in curator_figures; then, when the night shift may queue a run now (no run of the schedule queued or held, under the
night's runs and budget, the worker on duty live), it queues the night's review run (``queue_review``). Each pass also
opens again the proposals deferred until a moment that has passed. ``fire_schedules`` asks ``queue_review`` too before
it queues a plan run, so the review comes first in a night whichever job runs first.

The figures are counted without any model, over the REVIEW_DAYS (the charter's ``review.days``) before the moment they
are counted, from what the hub holds and the schedule's owner may read: per tool, the calls and failures of the runs
that ended (run_tool_stats) and of the sessions; per Bash program, those of the sessions, with their failures of a
wrong call (usage_errors) and of the environment (env_errors) as the sessions' digests tell them
(``evo_agents.hub.friction``); per skill (``skills``), its loads in the sessions' digests (typed with /name among them)
and in runs, the friction in those sessions from each load to the person's next turn (Bash commands, failures by kind,
interrupts, corrections) next to the baseline of every digest that counts skills, and what came of the runs that loaded
it (done, the steps they reported done after the worker ran their verify again, failed and lost by cause); the failures
that come from the environment, from the sessions' failed results, the failed tool calls of the runs and the errors of
the runs, by cause (``review.environment_cause``); the runs that failed or were lost, by cause: the one the worker
reported (runs.failure_cause), else the one read in the error (``review.run_cause``); the commands run again and again
across sessions; the turns of the person that correct the agent; the open items of the plans (tech_debt, debt,
open_questions); the steps of active plans that have not moved for STUCK_DAYS; the runs, sessions and decisions of the
span; and what the worker on duty last said of keeping the project's graph fresh (``kg_sync_figures``), a sync that
failed with its error;
and the health of the project's knowledge graph (``kg_health``, ``evo_agents.hub.server.kg_health``): how far each git
source lags its default branch and the files it lacks or holds stale, at the latest measure, what the builds of the span
covered, and what agents got from the graph; and the ``runtime`` and the ``harness`` of the sessions and runs
(``evo_agents.hub.server.runtime_figures``): their tokens, the prompt cache, the context, the compactions, the cost of
each session, run and merged pull request, the time of each hook and tool, and what the harness puts into every
context, by source, with the always-loaded files of each repo; and the ``product`` the lens product reads
(``evo_agents.hub.server.product_figures``): the active goals with their metrics, the signals of the span and the
canvases of a project that enables the plugin canvas. The sessions are their facets
(``evo_agents.hub.server.facet_figures``):
each session's whose transcripts or digest the owner may read, a digest the job hub.analyze_facets has not read yet as
its facet would be, and a local run's only when its member is the schedule's owner. The night's figures add ``weekly``,
the weekly series of every facet the owner may read, over the whole history, ``facets``, how far the facets of the
project are (``evo_agents.hub.facet_job.coverage``, and under ``llm`` those of the model), and ``clusters``, the groups
of the facets of the model of 3 sessions or more, counted again (``evo_agents.hub.server.history``). Each entry names
evidence a finding can cite as it is
(``session:ID:FIELD:INDEX``, ``run:ID:SEQ``). Learned skills waiting for review and the open items of reports live in
files, not on the hub: the worker counts them in the review run's worktrees (``evo_agents.worker.run.ReviewRun``). A
session, a plan, and the runs of a plan, that the schedule's owner may not read through the project's hub sink are left
out. The review run counts as one of the night's runs, and its cost as part of the night's. The same data counted again
gives the same figures: every list is ordered by its counts and then by keys.

The review run is a run of kind review, dispatched as the owner of the schedule (dispatched_via schedule), pinned to the
worker on duty, which must say it runs review runs, over the repos of the project the worker has a checkout of, with the
Reviewer's runtime and model of the charter and the night's caps, the review's own (``review.budget_usd``) below them.
A project has at most one review run a night, and one active at a time. Its prompt is ``review.build_review_prompt``
over the night's figures and the lenses of the night, which the run's first event names, and on a night of the lens
product the plugins it may propose through (PRODUCT_PLUGINS the project enables); ``looks_through`` tells those
plugins whether a run is such a review run.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, facet_job, kg_labels, review, runs, tables
from evo_agents.hub import kg_optimize as optimize_model
from evo_agents.hub.friction import SKILL_FIELDS
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import (
    audit,
    facet_figures,
    kg_health,
    kg_optimize,
    memory_promotion,
    product_figures,
    runtime_figures,
)
from evo_agents.hub.server import goals as project_goals
from evo_agents.hub.server import history as history_figures
from evo_agents.hub.server.facet_figures import Cause, cut, rate
from evo_agents.hub.server.night_duty import Duty
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import Pinned, insert_run, unfit
from evo_agents.hub.server.runs.service.kg_duties import keeper_of
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.tables_kg import OPTIMIZE_ACTIVE

log = logging.getLogger(__name__)

REVIEW = "curator.review"  # a schedule queued the night's review run: "<project> run:<id> night=<date> lenses=..."
STUCK_DAYS = 3  # a step of an active plan that has not moved for this long is stuck
MAX_FAILED_EVENTS = 2000  # failed tool calls of runs one count reads
TOP = 30  # entries of a list of figures
SAMPLES = facet_figures.SAMPLES  # runs, sessions and evidence an entry names
OPEN_ITEMS = 50
CLOSED = frozenset({"done", "fixed", "resolved", "answered", "closed", "wontfix", "dropped", "merged", "cancelled"})
DEBT_SECTIONS = ("tech_debt", "debt", "open_questions")
PRODUCT_PLUGINS = ("canvas", "builder")  # the plugins the lens product proposes through, where the project enables them
SHELL_TOOLS = frozenset({"Bash", "shell"})  # Claude Code's and Codex's tool of the commands a digest's bash counts
KIND_KEYS = ("usage_errors", "env_errors")  # the kinds of a program's failures the figures name
COMPOUND_KEYS = ("calls", "errors", "usage_errors", "env_errors", "other_errors")  # a digest's bash_compound
DIGEST_PARTS = ("tools", "bash_kinds", "bash_compound", "skills", "interrupts", "turns")  # what _skill_digests reads


def _ended_runs(project_id: int, plans: list[str], since: datetime, until: datetime):
    """The runs of the project that ended in the span, of a plan the owner may read or a review run; never a local run,
    which a member's session is: the owner's own count among the sessions, by their facets (``facet_figures``)."""
    r = tables.runs
    return (
        r.c.project_id == project_id,
        r.c.kind != "local",
        (r.c.plan_id.in_(plans)) | (r.c.kind == "review"),
        r.c.finished_at >= since,
        r.c.finished_at < until,
    )


def _list(value) -> list:
    return value if isinstance(value, list) else []


async def _run_figures(conn: AsyncConnection, ended: tuple, environment: dict[str, Cause]) -> dict:
    """tools (from the runs), failed_runs and runs; adds the environment causes of the runs' failures to
    ``environment``."""
    r, s, e = tables.runs, tables.run_tool_stats, tables.run_events
    tool_rows = await conn.execute(
        select(
            s.c.tool_name,
            r.c.runtime,
            func.sum(s.c.calls).label("calls"),
            func.sum(s.c.errors).label("errors"),
            func.count().label("runs"),
        )
        .join_from(s, r, r.c.id == s.c.run_id)
        .where(*ended)
        .group_by(s.c.tool_name, r.c.runtime)
        .order_by(s.c.tool_name, r.c.runtime)
    )
    tools = [
        {
            "tool": row.tool_name,
            "source": "runs",
            "runtime": row.runtime,
            "calls": int(row.calls),
            "errors": int(row.errors),
            "error_rate": rate(int(row.errors), int(row.calls)),
            "runs": row.runs,
        }
        for row in tool_rows
    ]
    usage_cost = curator.run_cost
    columns = (r.c.id, r.c.state, r.c.kind, r.c.error, r.c.failure_cause, r.c.usage)
    rows = (await conn.execute(select(*columns).where(*ended).order_by(r.c.id))).all()
    states, cost = Counter(row.state for row in rows), sum(usage_cost(row.usage) for row in rows)
    failed: dict[str, dict] = {}
    for row in rows:
        if row.state not in ("failed", "lost"):
            continue
        cause = review.run_cause(row.failure_cause, row.error, row.state)
        entry = failed.setdefault(cause, {"cause": cause, "failed": 0, "lost": 0, "runs": [], "sample": None})
        entry[row.state] += 1
        if len(entry["runs"]) < SAMPLES * 2:
            entry["runs"].append(row.id)
        entry["sample"] = entry["sample"] or (cut(row.error) if row.error else None)
        found = review.environment_cause(row.error or "")
        if found is not None:
            environment[found].add(1, run=row.id, evidence=f"run:{row.id}:1", sample=cut(row.error))
    text = e.c.body["content"][0]["content"]["text"].astext
    failures = await conn.execute(
        select(e.c.run_id, e.c.seq, text.label("text"))
        .join_from(e, r, r.c.id == e.c.run_id)
        .where(*ended, e.c.kind == "tool_call_update", e.c.body["status"].astext == "failed")
        .order_by(e.c.run_id.desc(), e.c.seq)
        .limit(MAX_FAILED_EVENTS)
    )
    for row in failures:
        found = review.environment_cause(row.text or "")
        if found is not None:
            environment[found].add(1, run=row.run_id, evidence=f"run:{row.run_id}:{row.seq}", sample=cut(row.text))
    return {
        "tools": tools,
        "failed_runs": sorted(failed.values(), key=lambda item: (-(item["failed"] + item["lost"]), item["cause"])),
        "runs": {**{state: states.get(state, 0) for state in runs.TERMINAL_STATES}, "cost_usd": round(cost, 4)},
    }


# The skills, and the kinds of the programs' failures


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


async def _skill_digests(conn: AsyncConnection, access: ProjectAccess, held: list) -> list:
    """The digests of the sessions of ``held`` (``facet_figures.sessions_of``) that the reader may read through the
    project's hub sink, with the parts the skills and the kinds of failures read (DIGEST_PARTS): a facet does not
    carry them, so they come from the digest the evo-hub plugin pushed."""
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    ids = sorted({session.session for session in held if session.session is not None})
    if through is None or not ids:
        return []
    d = tables.session_digests
    query = (
        select(d.c.session_id, d.c.label, *(d.c.body[name].label(name) for name in DIGEST_PARTS))
        .where(d.c.project_id == access.project_id, d.c.session_id.in_(ids))
        .order_by(d.c.session_id)
    )
    return [row for row in (await conn.execute(query)).all() if access.visible(row.label, through)]


def _with_kinds(programs: list[dict], rows) -> list[dict]:
    """``programs`` (of ``facet_figures.session_figures``) with the failures of each by kind from the digests that tell
    them (``bash_kinds``): usage_errors and env_errors; a digest of an older client adds none."""
    kinds: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        for item in _list(row.bash_kinds):
            if isinstance(item, dict) and item.get("program"):
                kinds[item["program"]].update(
                    usage_errors=_int(item.get("usage_errors")), env_errors=_int(item.get("env_errors"))
                )
    return [{**entry, **{key: kinds[entry["program"]][key] for key in KIND_KEYS}} for entry in programs]


def _baseline(rows) -> dict:
    """What every digest that counts skills says of all its turns: Bash commands and their failures (the Bash tool's,
    Codex's shell), the person's turns and interrupts."""
    base = Counter()
    for row in rows:
        if not isinstance(row.skills, list):
            continue  # a digest of a client that did not count skills
        base["digests"] += 1
        for item in _list(row.tools):
            if isinstance(item, dict) and item.get("gen_ai.tool.name") in SHELL_TOOLS:
                base.update(bash=_int(item.get("calls")), errors=_int(item.get("errors")))
        base.update(turns=_int(row.turns), interrupts=_int(row.interrupts))
        compound = row.bash_compound if isinstance(row.bash_compound, dict) else {}
        base.update({f"compound_{key}": _int(compound.get(key)) for key in COMPOUND_KEYS})
    return base


def _session_skills(rows) -> dict[str, dict]:
    """Per skill, what the digests count of it (``friction.SKILL_FIELDS``) with the sessions that loaded it, and
    evidence of them."""
    found: dict[str, dict] = {}
    for row in rows:
        for index, item in enumerate(_list(row.skills)):
            if not (isinstance(item, dict) and isinstance(item.get("skill"), str) and item["skill"]):
                continue
            entry = found.setdefault(item["skill"], {"counts": Counter(), "evidence": []})
            entry["counts"].update({key: _int(item.get(key)) for key in SKILL_FIELDS}, sessions=1)
            if len(entry["evidence"]) < SAMPLES:
                entry["evidence"].append(f"session:{row.session_id}:skills:{index}")
    return found


async def _run_skills(conn: AsyncConnection, ended: tuple) -> dict[str, dict]:
    """Per skill the Skill tool of the runs that ended loaded: its loads and failed loads, and what came of those runs:
    done, the steps they reported done (each after the worker ran its verify again), failed and lost by cause."""
    r, s, e = tables.runs, tables.run_tool_stats, tables.run_events
    query = select(s.c.run_id, s.c.skills, r.c.state, r.c.failure_cause, r.c.error).join_from(
        s, r, r.c.id == s.c.run_id
    )
    rows = (await conn.execute(query.where(*ended, s.c.skills.is_not(None)))).all()
    done = e.c.body["step_report"]["status"].astext == "done"
    steps = select(e.c.run_id, func.count().label("n")).where(
        e.c.run_id.in_([row.run_id for row in rows]), e.c.kind == "system", done
    )
    verified = dict((await conn.execute(steps.group_by(e.c.run_id))).all()) if rows else {}
    found: dict[str, dict] = {}
    for row in rows:
        cause = review.run_cause(row.failure_cause, row.error, row.state) if row.state in ("failed", "lost") else None
        for item in _list(row.skills):
            if not (isinstance(item, dict) and isinstance(item.get("skill"), str)):
                continue
            entry = found.setdefault(item["skill"], _run_entry())
            entry["calls"] += _int(item.get("calls"))
            entry["failed"] += _int(item.get("errors"))
            entry["runs"] += 1
            outcomes = entry["run_outcomes"]
            outcomes["done"] += row.state == "done"
            outcomes["verified_steps"] += verified.get(row.run_id, 0)
            if cause is not None:
                outcomes[row.state][cause] = outcomes[row.state].get(cause, 0) + 1
            if len(outcomes["run_ids"]) < SAMPLES:
                outcomes["run_ids"].append(row.run_id)
    return found


def _run_entry() -> dict:
    outcomes = {"done": 0, "verified_steps": 0, "failed": {}, "lost": {}, "run_ids": []}
    return {"calls": 0, "failed": 0, "runs": 0, "run_outcomes": outcomes}


def _skill_figures(sessions: dict[str, dict], ran: dict[str, dict], base: Counter) -> list[dict]:
    """``skills`` of the figures: per skill, its loads in sessions and runs, the friction after them in sessions next
    to the baseline of every session that counts skills, and what came of the runs that loaded it; the most loaded
    first."""
    entries = []
    for name in sessions.keys() | ran.keys():
        found = sessions.get(name) or {"counts": Counter(), "evidence": []}
        counted, run = found["counts"], ran.get(name) or _run_entry()
        errors = sum(counted[key] for key in ("usage_errors", "env_errors", "other_errors"))
        entries.append(
            {
                "skill": name,
                "calls": counted["calls"] + run["calls"],
                "typed": counted["typed"],
                "failed": counted["failed"] + run["failed"],
                "sessions": counted["sessions"],
                "runs": run["runs"],
                **{key: counted[key] for key in ("bash", "usage_errors", "env_errors", "other_errors")},
                "error_rate": rate(errors, counted["bash"]),
                "baseline_error_rate": rate(base["errors"], base["bash"]),
                "interrupts": counted["interrupts"],
                "corrections": counted["corrections"],
                "interrupt_rate": rate(counted["interrupts"], counted["calls"]),
                "baseline_interrupt_rate": rate(base["interrupts"], base["turns"]),
                "run_outcomes": run["run_outcomes"],
                "evidence": found["evidence"],
            }
        )
    return sorted(entries, key=lambda item: (-item["calls"], item["skill"]))[:TOP]


def _open(entry) -> bool:
    return not (isinstance(entry, dict) and str(entry.get("status") or "").lower() in CLOSED)


def _entry_text(entry) -> str:
    if not isinstance(entry, dict):
        return cut(entry)
    for key in ("title", "what", "question", "text", "item", "note"):
        if entry.get(key):
            return cut(entry[key])
    return cut(", ".join(f"{key}={value}" for key, value in entry.items()))


async def _plan_figures(conn: AsyncConnection, access: ProjectAccess, now: datetime) -> dict:
    """open_items of the plans the owner may read and the stuck_steps of the active ones."""
    p = tables.plans
    rows = await conn.execute(
        select(p.c.plan_id, p.c.area, p.c.label, p.c.body, p.c.updated_at)
        .where(p.c.project_id == access.project_id)
        .order_by(p.c.plan_id)
    )
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    open_items, stuck = [], []
    for row in rows:
        if through is None or not access.visible(row.label, through):
            continue
        body = row.body if isinstance(row.body, dict) else {}
        for section in DEBT_SECTIONS:
            for index, entry in enumerate(_list(body.get(section))):
                if _open(entry) and len(open_items) < OPEN_ITEMS:
                    key = entry.get("id", index) if isinstance(entry, dict) else index
                    open_items.append(
                        {"plan_id": row.plan_id, "section": section, "key": key, "text": _entry_text(entry)}
                    )
        idle = (now - row.updated_at).days
        if row.area != "active" or idle < STUCK_DAYS:
            continue
        for index, step in enumerate(_list(body.get("steps"))):
            if not isinstance(step, dict):
                continue
            ready = runs.unready_reason(body, step) is None
            if step.get("status") == "in_progress" or ready:
                stuck.append(
                    {
                        "plan_id": row.plan_id,
                        "step": str(step.get("id", index + 1)),
                        "status": step.get("status") or "pending",
                        "title": cut(step.get("title")),
                        "days": idle,
                    }
                )
    stuck.sort(key=lambda item: (-item["days"], item["plan_id"], item["step"]))
    return {"open_items": open_items, "stuck_steps": stuck[:TOP]}


async def _decision_figures(conn: AsyncConnection, project_id: int, plans: list[str], since: datetime) -> dict:
    d, r = tables.decisions, tables.runs
    rows = await conn.execute(
        select(d.c.state, d.c.category, func.count().label("n"))
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(d.c.project_id == project_id, r.c.kind != "local", d.c.plan_id.in_(plans), d.c.asked_at >= since)
        .group_by(d.c.state, d.c.category)
    )
    states, categories = Counter(), Counter()
    for row in rows:
        states[row.state] += row.n
        categories[row.category] += row.n
    return {"by_state": dict(sorted(states.items())), "by_category": dict(sorted(categories.items()))}


def _tool_order(item: dict) -> tuple:
    return (
        -item["errors"],
        -item["error_rate"],
        -item["calls"],
        item["tool"],
        item["source"],
        item.get("runtime") or "",
    )


async def _counted(
    conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime, owner_id: int | None
) -> tuple[dict, list, list[str]]:
    """(the figures of ``compute_figures``, the sessions they count, the plans the reader may read)."""
    plans = await visible_plans(conn, access, None)
    held = await facet_figures.sessions_of(conn, access, since, until, owner_id)
    sessions = facet_figures.session_figures(held)
    environment: dict[str, Cause] = sessions.pop("environment")
    ended = _ended_runs(access.project_id, plans, since, until)
    ran = await _run_figures(conn, ended, environment)
    digest_rows = await _skill_digests(conn, access, held)
    sessions["programs"] = _with_kinds(sessions["programs"], digest_rows)
    base = _baseline(digest_rows)
    skills = _skill_figures(_session_skills(digest_rows), await _run_skills(conn, ended), base)
    sessions["sessions"]["with_skills"] = base["digests"]
    sessions["sessions"]["compound_bash"] = {key: base[f"compound_{key}"] for key in COMPOUND_KEYS}
    tools = sorted(ran.pop("tools") + sessions.pop("tools"), key=_tool_order)[:TOP]
    causes = [
        {
            "cause": name,
            "count": found.count,
            "sessions": found.sessions,
            "runs": found.runs,
            "evidence": found.evidence,
            "samples": found.samples,
        }
        for name, found in sorted(environment.items(), key=lambda item: (-item[1].count, item[0]))
    ]
    planned = await _plan_figures(conn, access, until)
    spent, harness = await runtime_figures.figures(conn, access, ended, plans, since, until, held)
    figures = {
        "tools": tools,
        "environment": causes,
        **sessions,
        "skills": skills,
        **ran,
        **planned,
        "learned_skills": None,  # in files of the worktrees: the review run's worker counts them
        "decisions": await _decision_figures(conn, access.project_id, plans, since),
        "kg_health": await kg_health.figures(conn, access, plans, since, until, held),
        "runtime": spent,
        "harness": harness,
        "product": await product_figures.figures(conn, access, since, until),
    }
    return figures, held, plans


async def compute_figures(
    conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime, owner_id: int | None = None
) -> dict:
    """The figures of the project of ``access`` over [since, until), as its member ``owner_id`` (the owner of the
    charter, whose local runs count) reads it (see the module's docstring): a JSON object of the keys of
    ``review.FIGURE_KEYS`` but ``kg_sync``, ``weekly``, ``facets`` and ``clusters``, which the night's figures add
    (``ensure_figures``)."""
    return (await _counted(conn, access, since, until, owner_id))[0]


async def kg_sync_figures(conn: AsyncConnection, worker_id: int | None, project: str) -> dict | None:
    """What the worker on duty (the pool's node that keeps the graph, ``kg_duties.keeper``) last said of keeping the project's graph fresh (workers.kg_sync): when it looked, what
    stopped it, each git source with its commit, how far behind it was, when it was read and pushed and the error of a
    sync that failed, and how many failed; None when it said nothing of the project."""
    w = tables.workers
    row = (await conn.execute(select(w.c.name, w.c.kg_sync).where(w.c.id == worker_id))).one_or_none()
    report = row.kg_sync if row is not None and isinstance(row.kg_sync, dict) else {}
    entry = next((item for item in _list(report.get("projects")) if item.get("project") == project), None)
    if entry is None:
        return None
    sources = [item for item in _list(entry.get("sources")) if isinstance(item, dict)]
    failed = sum(1 for item in sources if item.get("status") == "failed") + (1 if entry.get("error") else 0)
    return {
        "worker": row.name,
        "checked_at": entry.get("checked_at"),
        "error": entry.get("error"),
        "failed": failed,
        "sources": sources[:TOP],
    }


# The night's row


def _night_row(project_id: int, night: date):
    cf = tables.curator_figures
    return select(cf.c.id, cf.c.figures, cf.c.run_id).where(cf.c.project_id == project_id, cf.c.night == night)


async def ensure_figures(conn: AsyncConnection, due, night: date, access: ProjectAccess):
    """The night's figures of schedule ``due``'s project: counted and kept the first time, read after that; the row
    (id, figures, run_id). The first time also pushes the night's signal ``kg.query.grep_after_rate``
    (``kg_labels.push_grep_after``)."""
    found = (await conn.execute(_night_row(due.project_id, night))).one_or_none()
    if found is not None:
        return found
    settings = due.body.get("review") or {}
    days = int(settings.get("days") or review.REVIEW_DAYS)
    until = (await conn.execute(select(func.now()))).scalar_one()
    since = until - timedelta(days=days)
    figures, held, plans = await _counted(conn, access, since, until, due.owner_id)
    waiting = [session for session in held if session.numbers is not None]
    read = await history_figures.figures(conn, access)
    figures = {
        "project": due.project,
        "night": night.isoformat(),
        "days": days,
        "since": since.isoformat(),
        "until": until.isoformat(),
        **figures,
        "kg_sync": await kg_sync_figures(conn, await keeper_of(conn, due.id), due.project),
        "weekly": await facet_figures.weekly(conn, access, plans, until, due.owner_id, waiting),
        "facets": {**await facet_job.coverage(conn, access.project_id), "llm": read["llm"]},
        "clusters": read["clusters"],
    }
    cf = tables.curator_figures
    values = {"project_id": due.project_id, "schedule_id": due.id, "night": night, "since": since, "until": until}
    stored = await conn.execute(pg_insert(cf).values(**values, figures=figures).on_conflict_do_nothing())
    log.info("night figures counted", extra={"project": due.project, "night": night.isoformat(), "days": days})
    if stored.rowcount:
        await kg_labels.push_grep_after(conn, due.project_id, due.owner_id, night, figures)
    return (await conn.execute(_night_row(due.project_id, night))).one()


# The review run


@dataclass(frozen=True)
class Gate:
    """What the night shift found when it may queue a run now: the night, its runs and cost, the caps of the next
    run, the owner's access and where its runs go: the worker on duty, or the ready nodes of the pool
    (``night_duty.Duty``)."""

    night: date
    figures: object  # curator NightFigures
    budget: dict
    access: ProjectAccess
    duty: Duty
    inside: bool = True  # inside the window; outside it, the gate is for the Curator's changes alone (shift_hours)


def _review_budget(charter: dict, budget: dict) -> dict | None:
    """The caps of the review run: the next run's, its cost under the review's own cap when the charter sets one."""
    cap = (charter.get("review") or {}).get("budget_usd")
    usd = budget["max_usd"] if cap is None else min(budget["max_usd"], cap)
    if usd < curator.MIN_RUN_USD:
        return None
    return {**budget, "max_usd": round(usd, 4)}


async def _review_repos(conn: AsyncConnection, project_id: int, project: str, worker: Pinned) -> list[dict]:
    """The repos of the project the worker on duty has a checkout of, in name order, each with no branch: a review
    run's worktrees are detached at origin's default branch. A worker that has none and clones what it lacks
    (runs.CLONES_CHECKOUTS) reviews every repo of the project, cloned before the run starts."""
    pr = tables.project_repos
    names = (await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))).all()
    held = [{"repo": name, "branch": None} for (name,) in names if f"{project}/{name}" in worker.checkouts]
    if held or runs.CLONES_CHECKOUTS not in worker.features:
        return held
    return [{"repo": name, "branch": None} for (name,) in names]


async def _review_place(conn: AsyncConnection, due, duty: Duty, runtime: str) -> tuple[list[dict], list[str]]:
    """The repos of the review run and what keeps it from a worker on duty: the repos of the first one that fits it,
    else the problems of the first one (``dispatch.unfit``), none fitting."""
    found = []
    for worker in duty.nodes:
        repos = await _review_repos(conn, due.project_id, due.project, worker)
        problems = unfit(worker, due.project, "review", [entry["repo"] for entry in repos], runtime)
        if not repos:
            problems.append(f"it has no checkout of a repo of project {due.project}")
        if not problems:
            return repos, []
        found.append((worker, repos, problems))
    worker, repos, problems = found[0]
    if duty.pool is not None:
        problems = [f"no ready node of pool {duty.pool} fits it: {worker.name}: {'; '.join(problems)}"]
    return repos, problems


async def queue_review(conn: AsyncConnection, due, gate: Gate, *, hold: bool = False) -> int | None:
    """Queue the night's review run of schedule ``due`` when it has none yet, counting the night's figures first;
    its id, or None when there is nothing to queue (reviewed already, a worker that runs no review run, no repo to
    review, no money left). Called under the schedule's row lock. Figures that cannot be counted are logged and queue
    no review, so the night shift goes on with its plans. Inside the window the night's KG optimize run starts here;
    with ``hold`` (a pass that measures the graphs, the job's) the review waits for its first measure."""
    try:
        async with conn.begin_nested():
            row = await ensure_figures(conn, due, gate.night, gate.access)
    except Exception:
        log.exception("the night's figures were not counted", extra={"project": due.project})
        return None
    if row.run_id is not None:
        return None
    optimized = None
    if gate.inside:
        try:
            async with conn.begin_nested():
                optimized = await kg_optimize.night_run(conn, due, gate.night)
        except Exception:  # the night's review goes on without its KG optimize run
            log.exception("the night's KG optimize run did not start", extra={"project": due.project})
        if hold and kg_optimize.holds_review(optimized, await conn.scalar(select(func.now()))):
            log.info("the review run waits for the KG optimize run's measure", extra={"project": due.project})
            return None
    charter, duty = due.body, gate.duty
    runtime = (charter.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    repos, problems = await _review_place(conn, due, duty, runtime)
    budget = _review_budget(charter, gate.budget)
    if problems or budget is None:
        why = "; ".join(problems) or "the night has too little money left"
        log.info("no review run tonight yet", extra={"project": due.project, "why": why})
        return None
    count = int((charter.get("review") or {}).get("lenses") or review.LENSES_PER_NIGHT)
    lenses = kg_optimize.with_lens(review.lenses_for(gate.night, count), optimized)
    values = {
        "kind": "review",
        "project_id": due.project_id,
        "plan_id": None,
        "title": f"Review of the night of {gate.night.isoformat()}",
        "plan_revision": None,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **duty.columns(),
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": (charter.get("reviewer") or {}).get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos,
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }
    try:
        run_id = await insert_run(conn, values)
    except IntegrityError as exc:  # another review run of the project is active
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            return None
        raise
    cf = tables.curator_figures
    figures = {**row.figures, "lenses": lenses}
    await conn.execute(update(cf).values(run_id=run_id, figures=figures).where(cf.c.id == row.id))
    if optimized is not None and optimized.state in OPTIMIZE_ACTIVE:
        await kg_optimize.link_agent_run(conn, optimized, run_id)
    caps = (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )
    text = (
        f"Queued by the night shift of project {due.project} (charter revision {due.revision}) as the review of the "
        f"night of {gate.night.isoformat()}, as {due.owner} on {duty.where}: lenses {', '.join(lenses)}; "
        f"{caps}."
    )
    event = {"text": text, "schedule_id": due.id, "night": gate.night.isoformat(), "lenses": lenses, **budget}
    await write_event(conn, run_id, event)
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=REVIEW,
        target=f"{due.project} run:{run_id} night={gate.night.isoformat()} lenses={','.join(lenses)}",
        project_id=due.project_id,
    )
    log.info("review run queued", extra={"project": due.project, "run_id": run_id, "night": gate.night.isoformat()})
    return run_id


async def queue_optimize_review(conn: AsyncConnection, due, gate: Gate, optimize_id: int) -> tuple[int | None, str]:
    """Queue the review run of KG optimize run ``optimize_id``, asked for now: the kg_health lens alone, at any hour,
    beside the night's runs, under the charter's review cost cap; (its id, ""), or (None, why)."""
    charter, duty = due.body, gate.duty
    runtime = (charter.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    repos, problems = await _review_place(conn, due, duty, runtime)
    budget = _review_budget(charter, gate.budget)
    if problems or budget is None:
        return None, "; ".join(problems) or "the night has too little money left"
    lenses = ["kg_health"]
    values = {
        "kind": "review",
        "project_id": due.project_id,
        "plan_id": None,
        "title": f"KG optimize of {due.project}",
        "plan_revision": None,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **duty.columns(),
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": (charter.get("reviewer") or {}).get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos,
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }
    try:
        async with conn.begin_nested():
            run_id = await insert_run(conn, values)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            return None, "another review run of the project is active"
        raise
    caps = (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )
    text = (
        f"Queued for KG optimize run #{optimize_id} of project {due.project}, asked for, as {due.owner} on "
        f"{duty.where}: lens kg_health; {caps}."
    )
    event = {"text": text, "schedule_id": due.id, "optimize_run_id": optimize_id, "lenses": lenses, **budget}
    await write_event(conn, run_id, event)
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=REVIEW,
        target=f"{due.project} run:{run_id} optimize={optimize_id} lenses=kg_health",
        project_id=due.project_id,
    )
    log.info("kg optimize review run queued", extra={"project": due.project, "run_id": run_id})
    return run_id, ""


async def _remembered(conn: AsyncConnection, owner_id: int, project_id: int, lenses: list[str]) -> tuple[dict, list]:
    """What earlier nights found through ``lenses`` and the playbook's lines, as the run's owner reads them."""
    from evo_agents.hub.server import earlier, history, playbook  # they read the proposals and runs of these routes

    access = await history.owner_access(conn, owner_id, project_id)
    if access is None:
        return {}, []
    return await earlier.for_prompt(conn, access, lenses), await playbook.for_prompt(conn, access, lenses)


async def _assigned(conn: AsyncConnection, owner_id: int, project_id: int, night: date | None) -> list[dict]:
    """What the weekly probe report asks of the review run of ``night`` (``probe_fixes.assignments``), as its owner
    reads the probes."""
    from evo_agents.hub.server import history, probe_fixes  # they read the proposals and runs of these routes
    from evo_agents.hub.server.security import MACHINE, Principal

    access = await history.owner_access(conn, owner_id, project_id)
    if access is None:
        return []
    login = (await conn.execute(select(tables.users.c.login).where(tables.users.c.id == owner_id))).scalar_one()
    owner = Principal(user_id=owner_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    night = night or await conn.scalar(select(func.current_date()))
    return [item.prompt() for item in await probe_fixes.assignments(conn, access, owner, night)]


async def review_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of review run ``view`` (a ``runs.Run``): the night's figures and lenses, the project's active goals
    (``goals.curator_goals``, those of the charter made goals first), what earlier nights found through the lenses,
    the playbook of the Curator, the agents' memories it may promote (``memory_promotion.for_prompt``) and what the
    weekly probe report assigns it (``probe_fixes.assignments``)."""

    r, c = tables.runs, tables.charters
    row = (
        await conn.execute(select(r.c.project_id, r.c.schedule_night, r.c.dispatched_by).where(r.c.id == view.id))
    ).one()
    figures = (await conn.execute(_night_row(row.project_id, row.schedule_night))).one_or_none()
    charter = (
        await conn.execute(
            select(c.c.body).where(c.c.project_id == row.project_id).order_by(c.c.revision.desc()).limit(1)
        )
    ).scalar_one_or_none() or {}
    held = figures.figures if figures is not None else {}
    lenses = held.get("lenses") or review.lenses_for(
        row.schedule_night, int((charter.get("review") or {}).get("lenses") or review.LENSES_PER_NIGHT)
    )
    repos = [repo.model_dump() for repo in view.repos or []]
    earlier, playbook = await _remembered(conn, row.dispatched_by, row.project_id, list(lenses))
    goals = await project_goals.curator_goals(conn, row.project_id, view.project)
    plugins = [name for name in PRODUCT_PLUGINS if await enabled_in(conn, name, row.project_id) is not None]
    memories = await memory_promotion.for_prompt(conn, view.id, row.project_id, row.dispatched_by)
    assignments = await _assigned(conn, row.dispatched_by, row.project_id, row.schedule_night)
    optimized = await kg_optimize.of_agent_run(conn, view.id)
    if optimized is not None and optimized.via == "manual":
        lenses = ["kg_health"]
    elif optimized is not None:
        lenses = kg_optimize.with_lens(list(lenses), SimpleNamespace(state="running"))
    return review.build_review_prompt(
        view.project,
        row.schedule_night,
        lenses,
        repos,
        held,
        goals,
        earlier=earlier,
        playbook=playbook,
        plugins=plugins,
        memories=memories,
        assignments=assignments,
        optimize=None if optimized is None else optimize_model.prompt_lines(optimized.id, optimized.body or {}),
    )


async def looks_through(conn: AsyncConnection, run_id: int, project_id: int, lens: str) -> bool:
    """Whether run ``run_id`` is a review run of project ``project_id``, not ended, whose night looks through
    ``lens``: the lenses its figures name (``queue_review``). The plugins canvas and builder ask it before they take
    a proposal of the lens product from the run."""
    r, cf = tables.runs, tables.curator_figures
    query = (
        select(cf.c.figures["lenses"])
        .join_from(r, cf, cf.c.run_id == r.c.id)
        .where(r.c.id == run_id, r.c.kind == "review", r.c.project_id == project_id, r.c.state.in_(runs.ACTIVE_STATES))
    )
    lenses = (await conn.execute(query)).scalar_one_or_none()
    return isinstance(lenses, list) and lens in lenses


# The job


async def reopen_deferred(conn: AsyncConnection) -> int:
    """Open again the proposals deferred until a moment that has passed; how many."""
    p = tables.proposals
    opened = await conn.execute(
        update(p)
        .values(state="open", deferred_until=None)
        .where(p.c.state == "deferred", p.c.deferred_until <= func.now())
        .returning(p.c.id)
    )
    return len(opened.all())


async def collect(
    engine: AsyncEngine, *, now: datetime | None = None, github_app=None, blobs=None, data_dir=None
) -> dict:
    """One pass of ``curator.collect`` over every night_shift schedule, each in a transaction of its own, at ``now``
    (the database's now when None); how many schedules ended in each outcome, and how many deferred proposals opened
    again. With ``data_dir``, the worker's cache (the job's), the graphs of the projects that are due are measured
    first (``kg_health.measure``, with ``github_app`` and ``blobs``), and ``graphs`` says how many."""
    from evo_agents.hub.server.curator import _due, gate_of  # it imports this module

    measured = None
    if data_dir is not None:
        try:
            measured = await kg_health.measure(engine, github_app=github_app, blobs=blobs, data_dir=data_dir, now=now)
        except Exception:  # the night's figures go on without a new measure of the graph
            log.exception("measuring the knowledge graphs failed")
            measured = {"failed": 1}
    optimized = None
    if data_dir is not None:
        try:
            optimized = await kg_optimize.advance(engine, blobs=blobs, data_dir=data_dir)  # by the database's clock
        except Exception:  # the night shift goes on without moving its KG optimize runs
            log.exception("moving the KG optimize runs on failed")
            optimized = {"failed": 1}
    async with engine.begin() as conn:
        due = (await conn.execute(_due(now))).all()
        reopened = await reopen_deferred(conn)
    outcomes: Counter = Counter()
    for row in due:
        try:
            async with engine.begin() as conn:
                word, gate = await gate_of(conn, row, figures_first=True)
                if gate is not None:
                    queued = await queue_review(conn, row, gate, hold=data_dir is not None)
                    word = "review" if queued is not None else "collected"
                outcomes[word] += 1
        except Exception:  # one schedule failing leaves the others their turn
            log.exception("collecting a night failed", extra={"project": row.project, "schedule_id": row.id})
            outcomes["failed"] += 1
    found = {**dict(sorted(outcomes.items())), "reopened": reopened}
    if optimized:
        found["kg_optimize"] = optimized
    return found if measured is None else {**found, "graphs": measured}
