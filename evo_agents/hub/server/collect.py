"""The night's figures of a project and its review run: the job ``curator.collect`` and what ``fire_schedules`` asks
of it. ``evo_agents.hub.review`` holds the model (lenses, causes, the prompt), ``evo_agents.hub.server.curator`` the
night shift whose schedule queues the run, and ``evo_agents.hub.server.proposals`` what the run writes.

``collect`` is the job ``curator.collect``, every minute: for each night_shift schedule that is not paused, inside its
charter's window, under the schedule's row lock (a schedule another job holds is passed over), it counts the night's
figures once (``ensure_figures``), as the owner of the schedule may read the project, and keeps them in
curator_figures; then, when the night shift may queue a run now (no run of the schedule queued or held, under the
night's runs and budget, the worker on duty live), it queues the night's review run (``queue_review``). Each pass also
opens again the proposals deferred until a moment that has passed. ``fire_schedules`` asks ``queue_review`` too before
it queues a plan run, so the review comes first in a night whichever job runs first.

The figures are counted without any model, over the REVIEW_DAYS (the charter's ``review.days``) before the moment they
are counted, from what the hub holds and the schedule's owner may read: per tool, the calls and failures of the runs
that ended (run_tool_stats) and of the session digests pushed; per Bash program, those of the digests, with their
failures of a wrong call (usage_errors) and of the environment (env_errors) as the digests tell them
(``evo_agents.hub.friction``); per skill (``skills``), its loads in sessions (typed with /name among them) and in runs,
the friction in the sessions from each load to the person's next turn (Bash commands, failures by kind, interrupts,
corrections) next to the baseline of every session that counts skills, and what came of the runs that loaded it (done,
the steps they reported done after the worker ran their verify again, failed and lost by cause); the failures
that come from the environment, from the digests' failed results, the failed tool calls of the runs and the errors of
the runs, by cause (``review.environment_cause``); the runs that failed or were lost, by cause: the one the worker
reported (runs.failure_cause), else the one read in the error (``review.run_cause``); the commands run again and again
across sessions; the turns of the person that correct the agent; the open items of the plans (tech_debt, debt,
open_questions); the steps of active plans that have not moved for STUCK_DAYS; the runs, sessions and decisions of the
span. Each entry names evidence a finding can cite as it is (``session:ID:FIELD:INDEX``, ``run:ID:SEQ``). Learned
skills waiting for review and the open items of reports live in files, not on the hub: the worker counts them in the
review run's worktrees (``evo_agents.worker.run.ReviewRun``). A digest, a plan, and the runs of a plan, that the
schedule's owner may not read through the project's hub sink are left out. The review run counts as one of the night's
runs, and its cost as part of the night's.

The review run is a run of kind review, dispatched as the owner of the schedule (dispatched_via schedule), pinned to the
worker on duty, which must say it runs review runs, over the repos of the project the worker has a checkout of, with the
Reviewer's runtime and model of the charter and the night's caps, the review's own (``review.budget_usd``) below them.
A project has at most one review run a night, and one active at a time. Its prompt is ``review.build_review_prompt``
over the night's figures and the lenses of the night, which the run's first event names.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import psycopg
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, review, runs, tables
from evo_agents.hub.friction import SKILL_FIELDS
from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import Pinned, insert_run, unfit
from evo_agents.hub.server.runs.service.views import visible_plans

log = logging.getLogger(__name__)

REVIEW = "curator.review"  # a schedule queued the night's review run: "<project> run:<id> night=<date> lenses=..."
STUCK_DAYS = 3  # a step of an active plan that has not moved for this long is stuck
MAX_DIGESTS = 1000  # session digests one count reads, the latest pushed first
MAX_FAILED_EVENTS = 2000  # failed tool calls of runs one count reads
TOP = 30  # entries of a list of figures
SAMPLES = 5  # runs, sessions and evidence an entry names
SAMPLE_CHARS = 200
OPEN_ITEMS = 50
CLOSED = frozenset({"done", "fixed", "resolved", "answered", "closed", "wontfix", "dropped", "merged", "cancelled"})
DEBT_SECTIONS = ("tech_debt", "debt", "open_questions")
SHELL_TOOLS = frozenset({"Bash", "shell"})  # Claude Code's and Codex's tool of the commands a digest's bash counts
KIND_KEYS = ("usage_errors", "env_errors")  # the kinds of a program's failures the figures name
COMPOUND_KEYS = ("calls", "errors", "usage_errors", "env_errors", "other_errors")  # a digest's bash_compound


def _cut(text, limit: int = SAMPLE_CHARS) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def _rate(errors: int, calls: int) -> float:
    return round(errors / calls, 4) if calls else 0.0


# The figures


@dataclass
class _Cause:
    """The failures of one environment cause: how many, and a few of the sessions, runs, evidence and texts."""

    count: int = 0
    sessions: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    samples: list = field(default_factory=list)

    def add(self, count: int, *, session: str | None = None, run: int | None = None, evidence: str, sample: str):
        self.count += count
        for listed, value in ((self.sessions, session), (self.runs, run), (self.evidence, evidence)):
            if value is not None and value not in listed and len(listed) < SAMPLES:
                listed.append(value)
        if sample and sample not in self.samples and len(self.samples) < 3:
            self.samples.append(sample)


def _digests(project_id: int, since: datetime, until: datetime):
    d = tables.session_digests
    body = d.c.body
    return (
        select(
            d.c.session_id,
            d.c.label,
            d.c.messages,
            d.c.model,
            body["tools"].label("tools"),
            body["bash"].label("bash"),
            body["errors"].label("errors"),
            body["repeated_commands"].label("repeated_commands"),
            body["commands"].label("commands"),
            body["user_turns"].label("user_turns"),
            body["bash_kinds"].label("bash_kinds"),
            body["bash_compound"].label("bash_compound"),
            body["skills"].label("skills"),
            body["interrupts"].label("interrupts"),
            body["turns"].label("turns"),
        )
        .where(d.c.project_id == project_id, d.c.updated_at >= since, d.c.updated_at < until)
        .order_by(d.c.updated_at.desc())
        .limit(MAX_DIGESTS)
    )


def _ended_runs(project_id: int, plans: list[str], since: datetime, until: datetime):
    """The runs of the project that ended in the span, of a plan the owner may read or a review run."""
    r = tables.runs
    return (
        r.c.project_id == project_id,
        (r.c.plan_id.in_(plans)) | (r.c.kind == "review"),
        r.c.finished_at >= since,
        r.c.finished_at < until,
    )


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _session_figures(rows) -> dict:
    """tools, programs, environment (from the digests), repeated_commands, corrections and sessions, from the rows of
    ``_digests``."""
    tools, programs = defaultdict(Counter), defaultdict(Counter)
    environment: dict[str, _Cause] = defaultdict(_Cause)
    repeated: dict[str, dict] = {}
    corrections, messages, models = [], 0, Counter()
    for row in rows:
        session = row.session_id
        messages += row.messages or 0
        if row.model:
            models[row.model] += 1
        for item in _list(row.tools):
            if isinstance(item, dict) and item.get("gen_ai.tool.name"):
                found = tools[item["gen_ai.tool.name"]]
                found["calls"] += int(item.get("calls") or 0)
                found["errors"] += int(item.get("errors") or 0)
                found["sessions"] += 1
        for item in _list(row.bash):
            if isinstance(item, dict) and item.get("program"):
                found = programs[item["program"]]
                found["calls"] += int(item.get("calls") or 0)
                found["errors"] += int(item.get("errors") or 0)
        for index, item in enumerate(_list(row.errors)):
            if not isinstance(item, dict):
                continue
            cause = review.environment_cause(str(item.get("text") or ""))
            if cause is not None:
                environment[cause].add(
                    int(item.get("n") or 1),
                    session=session,
                    evidence=f"session:{session}:errors:{index}",
                    sample=_cut(item.get("text")),
                )
        seen = set()
        for index, item in enumerate(_list(row.repeated_commands)):
            if isinstance(item, dict) and item.get("command"):
                key = review.normalize_command(item["command"])
                entry = repeated.setdefault(key, {"command": _cut(key), "times": 0, "sessions": 0, "evidence": []})
                entry["times"] += int(item.get("n") or 0)
                if key not in seen:
                    entry["sessions"] += 1
                    seen.add(key)
                    if len(entry["evidence"]) < SAMPLES:
                        entry["evidence"].append(f"session:{session}:repeated_commands:{index}")
        for index, command in enumerate(_list(row.commands)):
            key = review.normalize_command(str(command))
            if key in seen or not key:
                continue
            seen.add(key)
            entry = repeated.setdefault(key, {"command": _cut(key), "times": 0, "sessions": 0, "evidence": []})
            entry["times"] += 1
            entry["sessions"] += 1
            if len(entry["evidence"]) < SAMPLES:
                entry["evidence"].append(f"session:{session}:commands:{index}")
        for index, turn in enumerate(_list(row.user_turns)):
            if isinstance(turn, str) and review.is_correction(turn) and len(corrections) < TOP:
                corrections.append(
                    {"session": session, "text": _cut(turn), "evidence": f"session:{session}:user_turns:{index}"}
                )
    return {
        "tools": [
            {
                "tool": name,
                "source": "sessions",
                "calls": found["calls"],
                "errors": found["errors"],
                "error_rate": _rate(found["errors"], found["calls"]),
                "sessions": found["sessions"],
            }
            for name, found in tools.items()
        ],
        "programs": sorted(
            (
                {
                    "program": name,
                    "calls": found["calls"],
                    "errors": found["errors"],
                    "error_rate": _rate(found["errors"], found["calls"]),
                }
                for name, found in programs.items()
                if found["errors"]
            ),
            key=lambda item: (-item["errors"], -item["calls"], item["program"]),
        )[:TOP],
        "environment": environment,
        "repeated_commands": sorted(
            (entry for entry in repeated.values() if entry["sessions"] >= 2 or entry["times"] >= 3),
            key=lambda item: (-item["sessions"], -item["times"], item["command"]),
        )[:TOP],
        "corrections": corrections,
        "sessions": {
            "digests": len(rows),
            "messages": messages,
            "models": [{"model": name, "sessions": n} for name, n in models.most_common(5)],
        },
    }


async def _run_figures(conn: AsyncConnection, ended: tuple, environment: dict[str, _Cause]) -> dict:
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
    )
    tools = [
        {
            "tool": row.tool_name,
            "source": "runs",
            "runtime": row.runtime,
            "calls": int(row.calls),
            "errors": int(row.errors),
            "error_rate": _rate(int(row.errors), int(row.calls)),
            "runs": row.runs,
        }
        for row in tool_rows
    ]
    usage_cost = curator.run_cost
    columns = (r.c.id, r.c.state, r.c.kind, r.c.error, r.c.failure_cause, r.c.usage)
    rows = (await conn.execute(select(*columns).where(*ended))).all()
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
        entry["sample"] = entry["sample"] or (_cut(row.error) if row.error else None)
        found = review.environment_cause(row.error or "")
        if found is not None:
            environment[found].add(1, run=row.id, evidence=f"run:{row.id}:1", sample=_cut(row.error))
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
            environment[found].add(1, run=row.run_id, evidence=f"run:{row.run_id}:{row.seq}", sample=_cut(row.text))
    return {
        "tools": tools,
        "failed_runs": sorted(failed.values(), key=lambda item: (-(item["failed"] + item["lost"]), item["cause"])),
        "runs": {**{state: states.get(state, 0) for state in runs.TERMINAL_STATES}, "cost_usd": round(cost, 4)},
    }


# The skills, and the kinds of the programs' failures


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _with_kinds(programs: list[dict], rows) -> list[dict]:
    """``programs`` (of ``_session_figures``) with the failures of each by kind from the digests that tell them
    (``bash_kinds``): usage_errors and env_errors; a digest of an older client adds none."""
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
                "error_rate": _rate(errors, counted["bash"]),
                "baseline_error_rate": _rate(base["errors"], base["bash"]),
                "interrupts": counted["interrupts"],
                "corrections": counted["corrections"],
                "interrupt_rate": _rate(counted["interrupts"], counted["calls"]),
                "baseline_interrupt_rate": _rate(base["interrupts"], base["turns"]),
                "run_outcomes": run["run_outcomes"],
                "evidence": found["evidence"],
            }
        )
    return sorted(entries, key=lambda item: (-item["calls"], item["skill"]))[:TOP]


def _open(entry) -> bool:
    return not (isinstance(entry, dict) and str(entry.get("status") or "").lower() in CLOSED)


def _entry_text(entry) -> str:
    if not isinstance(entry, dict):
        return _cut(entry)
    for key in ("title", "what", "question", "text", "item", "note"):
        if entry.get(key):
            return _cut(entry[key])
    return _cut(", ".join(f"{key}={value}" for key, value in entry.items()))


async def _plan_figures(conn: AsyncConnection, access: ProjectAccess, now: datetime) -> dict:
    """open_items of the plans the owner may read and the stuck_steps of the active ones."""
    p = tables.plans
    rows = await conn.execute(
        select(p.c.plan_id, p.c.area, p.c.label, p.c.body, p.c.updated_at).where(p.c.project_id == access.project_id)
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
                        "title": _cut(step.get("title")),
                        "days": idle,
                    }
                )
    stuck.sort(key=lambda item: (-item["days"], item["plan_id"], item["step"]))
    return {"open_items": open_items, "stuck_steps": stuck[:TOP]}


async def _decision_figures(conn: AsyncConnection, project_id: int, plans: list[str], since: datetime) -> dict:
    d = tables.decisions
    rows = await conn.execute(
        select(d.c.state, d.c.category, func.count().label("n"))
        .where(d.c.project_id == project_id, d.c.plan_id.in_(plans), d.c.asked_at >= since)
        .group_by(d.c.state, d.c.category)
    )
    states, categories = Counter(), Counter()
    for row in rows:
        states[row.state] += row.n
        categories[row.category] += row.n
    return {"by_state": dict(sorted(states.items())), "by_category": dict(sorted(categories.items()))}


async def compute_figures(conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime) -> dict:
    """The figures of the project of ``access`` over [since, until), as its member reads it (see the module's
    docstring): a JSON object of the keys of ``review.FIGURE_KEYS``."""
    plans = await visible_plans(conn, access, None)
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    digest_rows = [
        row
        for row in (await conn.execute(_digests(access.project_id, since, until))).all()
        if through is not None and access.visible(row.label, through)
    ]
    sessions = _session_figures(digest_rows)
    sessions["programs"] = _with_kinds(sessions["programs"], digest_rows)
    environment: dict[str, _Cause] = sessions.pop("environment")
    ended = _ended_runs(access.project_id, plans, since, until)
    ran = await _run_figures(conn, ended, environment)
    base = _baseline(digest_rows)
    skills = _skill_figures(_session_skills(digest_rows), await _run_skills(conn, ended), base)
    sessions["sessions"]["with_skills"] = base["digests"]
    sessions["sessions"]["compound_bash"] = {key: base[f"compound_{key}"] for key in COMPOUND_KEYS}
    tools = sorted(
        ran.pop("tools") + sessions.pop("tools"),
        key=lambda item: (-item["errors"], -item["error_rate"], -item["calls"], item["tool"]),
    )[:TOP]
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
    return {
        "tools": tools,
        "environment": causes,
        **sessions,
        "skills": skills,
        **ran,
        **planned,
        "learned_skills": None,  # in files of the worktrees: the review run's worker counts them
        "decisions": await _decision_figures(conn, access.project_id, plans, since),
    }


# The night's row


def _night_row(project_id: int, night: date):
    cf = tables.curator_figures
    return select(cf.c.id, cf.c.figures, cf.c.run_id).where(cf.c.project_id == project_id, cf.c.night == night)


async def ensure_figures(conn: AsyncConnection, due, night: date, access: ProjectAccess):
    """The night's figures of schedule ``due``'s project: counted and kept the first time, read after that; the row
    (id, figures, run_id)."""
    found = (await conn.execute(_night_row(due.project_id, night))).one_or_none()
    if found is not None:
        return found
    settings = due.body.get("review") or {}
    days = int(settings.get("days") or review.REVIEW_DAYS)
    until = (await conn.execute(select(func.now()))).scalar_one()
    since = until - timedelta(days=days)
    figures = await compute_figures(conn, access, since, until)
    figures = {
        "project": due.project,
        "night": night.isoformat(),
        "days": days,
        "since": since.isoformat(),
        "until": until.isoformat(),
        **figures,
    }
    cf = tables.curator_figures
    values = {"project_id": due.project_id, "schedule_id": due.id, "night": night, "since": since, "until": until}
    await conn.execute(pg_insert(cf).values(**values, figures=figures).on_conflict_do_nothing())
    log.info("night figures counted", extra={"project": due.project, "night": night.isoformat(), "days": days})
    return (await conn.execute(_night_row(due.project_id, night))).one()


# The review run


@dataclass(frozen=True)
class Gate:
    """What the night shift found when it may queue a run now: the night, its runs and cost, the caps of the next
    run, the owner's access and the worker on duty."""

    night: date
    figures: object  # curator NightFigures
    budget: dict
    access: ProjectAccess
    worker: Pinned


def _review_budget(charter: dict, budget: dict) -> dict | None:
    """The caps of the review run: the next run's, its cost under the review's own cap when the charter sets one."""
    cap = (charter.get("review") or {}).get("budget_usd")
    usd = budget["max_usd"] if cap is None else min(budget["max_usd"], cap)
    if usd < curator.MIN_RUN_USD:
        return None
    return {**budget, "max_usd": round(usd, 4)}


async def _review_repos(conn: AsyncConnection, project_id: int, project: str, worker: Pinned) -> list[dict]:
    """The repos of the project the worker on duty has a checkout of, in name order, each with no branch: a review
    run's worktrees are detached at origin's default branch."""
    pr = tables.project_repos
    names = (await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))).scalars()
    return [{"repo": name, "branch": None} for name in names if f"{project}/{name}" in worker.checkouts]


async def queue_review(conn: AsyncConnection, due, gate: Gate) -> int | None:
    """Queue the night's review run of schedule ``due`` when it has none yet, counting the night's figures first;
    its id, or None when there is nothing to queue (reviewed already, a worker that runs no review run, no repo to
    review, no money left). Called under the schedule's row lock. Figures that cannot be counted are logged and queue
    no review, so the night shift goes on with its plans."""
    try:
        async with conn.begin_nested():
            row = await ensure_figures(conn, due, gate.night, gate.access)
    except Exception:
        log.exception("the night's figures were not counted", extra={"project": due.project})
        return None
    if row.run_id is not None:
        return None
    charter, worker = due.body, gate.worker
    repos = await _review_repos(conn, due.project_id, due.project, worker)
    runtime = (charter.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    problems = unfit(worker, due.project, "review", [entry["repo"] for entry in repos], runtime)
    if not repos:
        problems.append(f"it has no checkout of a repo of project {due.project}")
    budget = _review_budget(charter, gate.budget)
    if problems or budget is None:
        why = "; ".join(problems) or "the night has too little money left"
        log.info("no review run tonight yet", extra={"project": due.project, "why": why})
        return None
    count = int((charter.get("review") or {}).get("lenses") or review.LENSES_PER_NIGHT)
    lenses = review.lenses_for(gate.night, count)
    values = {
        "kind": "review",
        "project_id": due.project_id,
        "plan_id": None,
        "title": f"Review of the night of {gate.night.isoformat()}",
        "plan_revision": None,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        "pinned_worker_id": worker.id,
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
    caps = (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )
    text = (
        f"Queued by the night shift of project {due.project} (charter revision {due.revision}) as the review of the "
        f"night of {gate.night.isoformat()}, as {due.owner} on worker {worker.name}: lenses {', '.join(lenses)}; "
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


async def review_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of review run ``view`` (a ``runs.Run``): the night's figures and lenses, the charter's goals."""
    r, c = tables.runs, tables.charters
    row = (await conn.execute(select(r.c.project_id, r.c.schedule_night).where(r.c.id == view.id))).one()
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
    return review.build_review_prompt(view.project, row.schedule_night, lenses, repos, held, charter.get("goals"))


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


async def collect(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """One pass of ``curator.collect`` over every night_shift schedule, each in a transaction of its own, at ``now``
    (the database's now when None); how many schedules ended in each outcome, and how many deferred proposals opened
    again."""
    from evo_agents.hub.server.curator import _due, gate_of  # it imports this module

    async with engine.begin() as conn:
        due = (await conn.execute(_due(now))).all()
        reopened = await reopen_deferred(conn)
    outcomes: Counter = Counter()
    for row in due:
        try:
            async with engine.begin() as conn:
                word, gate = await gate_of(conn, row, figures_first=True)
                if gate is not None:
                    word = "review" if await queue_review(conn, row, gate) is not None else "collected"
                outcomes[word] += 1
        except Exception:  # one schedule failing leaves the others their turn
            log.exception("collecting a night failed", extra={"project": row.project, "schedule_id": row.id})
            outcomes["failed"] += 1
    return {**dict(sorted(outcomes.items())), "reopened": reopened}
