"""The questions core plugins ask their agent types, and the question runs that answer them
(``evo_agents.hub.question_runs``; ``docs/plugins.md``, Agent types).

``ask`` is ``core_plugin_api.ask_agent_type`` without its handles, in the plugin's transaction: it checks, in this
order, the question, its asker and ref (422), the project and the plugin enabled there (404), the agent type (404), a
ref the plugin asked before (that question again, ``made`` false, nothing else checked), the type's profile and
``run_as`` (409), a worker of ``run_as`` the type may run on that takes question runs (409, each worker's reasons), the
clearance of the place the answer goes (403, ``core_plugin_api.restricted``) and the cap the type's runs share (429).
Then it queues a run of kind question dispatched by ``run_as`` with the credential kind machine, under the type's
profile, in every repo of the project with no branch, with one attempt and what is left of the cap as its cost cap,
keeps the question in agent_questions and audits ``<plugin>.ask`` in the project, ``run_as`` its actor.

``answer_of`` is ``core_plugin_api.read_agent_answer``: a question of the plugin by its id, its clearance checked again.
``question_prompt`` is the prompt of a question run at its claim; ``keep_answer`` keeps the answer of the worker's
report of done (``runs.service.reports``); ``question_run_ended`` stores the outcome and the event agent.answer for the
type's plugin when the run ends (``run_state.record_move``), the clearance checked again; ``expire_queued`` fails a
question run no worker claimed in QUEUE_SECONDS (``run_state.recover_runs``).
"""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import plugin_calls, plugin_events, profiles, question_runs, tables, task_runs
from evo_agents.hub.question_runs import MAX_ASKER_CHARS, MAX_REF_CHARS
from evo_agents.hub.server import audit, member_channels
from evo_agents.hub.server.agent_types import enabled_type, type_spend
from evo_agents.hub.server.chat_runs import project_repos
from evo_agents.hub.server.plugin_routes import not_enabled
from evo_agents.hub.server.profile_store import by_id
from evo_agents.hub.server.run_state import move_run, notify_queued
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.runs.service.worker_fit import worker_problems, workers_of
from evo_agents.hub.server.task_dispatch import type_refusal

log = logging.getLogger(__name__)

BUSY = "has no free slot"  # a worker that is only busy takes the question once a slot frees
NO_QUESTION = "the plugin asked no question {id}"
NO_WORKER = (
    "no worker claimed the question run in {seconds} seconds: no worker of {login} the agent type may run on was "
    "free and took question runs"
)


def _where(level: str, location: str | None, below: bool) -> str:
    return f"{'below ' if below else ''}{level}/{location or 'any location'}"


def label_of(name: str, default_label) -> dict:
    """The label of an answer of an agent of project ``name``: the project's default label, naming it alone."""
    return {**(default_label if isinstance(default_label, dict) else {}), "projects": [name]}


# Asking


async def _asked(conn: AsyncConnection, plugin_id: int, project_id: int, ref: str):
    q = tables.agent_questions
    query = select(q.c.id, q.c.run_id, q.c.agent_type_id).where(
        q.c.plugin_id == plugin_id, q.c.project_id == project_id, q.c.ref == ref
    )
    return (await conn.execute(query)).one_or_none()


async def _fit_problem(conn: AsyncConnection, enabled, row) -> str | None:
    """Why no worker of the type's run_as that the type may run on can take a question run now, or None: one that is
    only busy can, since the run waits in the queue."""
    profile = await by_id(conn, row.profile_id)
    named = {name.lower() for name in row.workers or ()}
    found = (await conn.execute(workers_of(row.run_as_id, enabled.project_id))).all()
    mine = [w for w in found if w.owner_id == row.run_as_id and (not named or w.name.lower() in named)]
    said = []
    for worker in mine:
        problems = [why for why in worker_problems(worker, profile, "machine", "question") if why != BUSY]
        if not problems:
            return None
        said.append(f"{worker.name} {', '.join(problems)}")
    which = f"of {', '.join(sorted(named))} " if named else ""
    return f"no worker {which}of {row.run_as}, whom agent type {row.name} runs as, can take a question run now: " + (
        "; ".join(said) if said else f"{row.run_as} has none that serves project {enabled.project}"
    )


async def _kept(conn: AsyncConnection, project: str, level: str, location: str | None, below: bool) -> bool:
    rules = await member_channels.rules_of(conn, project)
    return member_channels.kept_from(rules, level, location, below)


async def _queue(conn: AsyncConnection, enabled, row, question: str, left: float) -> int:
    """Queue the question run of ``question`` for agent type ``row`` in each repo of the project, on no branch."""
    profile = await by_id(conn, row.profile_id)
    allow = profiles.allowed(profile.permissions, "model")
    names = await project_repos(conn, enabled.project_id)
    values = {
        "kind": "question",
        "project_id": enabled.project_id,
        "title": question_runs.title_of(row.name),
        "dispatched_by": row.run_as_id,
        "dispatched_via": "machine",
        "requested_runtime": "any",
        "runtime": "any",
        "model": allow[0] if allow else None,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": question_runs.TIMEOUT_MIN * 60,
        "profile_id": profile.id,
        "profile_version": profile.version,
        "budget": question_runs.run_budget(left),
        "request": question,
        "agent_type_id": row.id,
        "max_attempts": question_runs.MAX_ATTEMPTS,
        "repos": [{"repo": name} for name in names] or null(),  # SQL NULL, not a JSON null
    }
    run_id = await insert_run(conn, values)
    await notify_queued(conn, run_id)
    return run_id


def _input_problem(question: str, asker: str, ref: str) -> str | None:
    return (
        question_runs.question_problem(question)
        or question_runs.line_problem("asker", asker, MAX_ASKER_CHARS)
        or question_runs.line_problem("ref", ref, MAX_REF_CHARS)
    )


async def ask(
    conn: AsyncConnection,
    plugin: str,
    project: str,
    name: str,
    question: str,
    *,
    asker: str,
    ref: str,
    cleared: tuple[str, str | None, bool],
) -> dict:
    """Core plugin ``plugin`` asks its agent type ``name`` of ``project`` ``question`` for ``asker``, its key ``ref``,
    the answer to go to a place cleared for ``cleared`` (level, location, below): {question_id, run_id, project,
    agent_type, ref, made}, or HTTPException with the status of the first refusal (the module's docstring)."""
    problem = _input_problem(question, asker, ref)
    if problem is not None:
        raise HTTPException(422, problem)
    p = tables.projects
    project_id = (await conn.execute(select(p.c.id).where(p.c.name == project))).scalar_one_or_none()
    enabled = None if project_id is None else await plugin_calls.enabled_in(conn, plugin, project_id)
    if enabled is None:
        raise not_enabled(plugin, project)
    row = await enabled_type(conn, project_id, enabled.id, name, lock=True)  # one question of a type at once
    refusal = await type_refusal(conn, enabled, row, name)
    if refusal is not None and refusal[0] == 404:
        raise HTTPException(*refusal)
    before = await _asked(conn, enabled.id, project_id, ref)
    if before is not None:
        return _asked_view(before, project, name, ref, made=False)
    if refusal is not None:
        raise HTTPException(*refusal)
    problem = await _fit_problem(conn, enabled, row)
    if problem is not None:
        raise HTTPException(409, problem)
    if await _kept(conn, project, *cleared):
        raise HTTPException(
            403,
            f"project {project} is kept from a place cleared for {_where(*cleared)}: an answer of its agents may not "
            "go there",
        )
    spent = await type_spend(conn, row.id)
    problem = question_runs.spent_problem(spent, row.daily_usd, name, task_runs.BUDGET_HOURS)
    if problem is not None:
        raise HTTPException(429, problem)
    return await _made(conn, enabled, row, question, asker=asker, ref=ref, cleared=cleared, left=row.daily_usd - spent)


async def _made(conn: AsyncConnection, enabled, row, question: str, *, asker, ref, cleared, left: float) -> dict:
    """Queue the run of a new question, keep the question and audit it."""
    run_id = await _queue(conn, enabled, row, question, left)
    level, location, below = cleared
    q = tables.agent_questions
    values = {"plugin_id": enabled.id, "project_id": enabled.project_id, "agent_type_id": row.id, "run_id": run_id}
    values |= {"ref": ref, "asker": asker, "cleared": {"level": level, "location": location, "below": below}}
    question_id = (await conn.execute(insert(q).values(values).returning(q.c.id))).scalar_one()
    target = (
        f"{enabled.project}/plugin:{enabled.name} agent-type:{row.name} question:{question_id} run:{run_id} "
        f"asker:{asker}"
    )
    action = f"{enabled.name}.ask"
    await audit.record(
        conn, actor_id=row.run_as_id, token_id=None, action=action, target=target, project_id=enabled.project_id
    )
    log.info(
        "question asked",
        extra={"project": enabled.project, "plugin": enabled.name, "agent_type": row.name, "run_id": run_id},
    )
    return {
        "question_id": question_id,
        "run_id": run_id,
        "project": enabled.project,
        "agent_type": row.name,
        "ref": ref,
        "made": True,
    }


def _asked_view(before, project: str, name: str, ref: str, *, made: bool) -> dict:
    return {
        "question_id": before.id,
        "run_id": before.run_id,
        "project": project,
        "agent_type": name,
        "ref": ref,
        "made": made,
    }


# Reading


def _question_select():
    """A question with its run's state and failure cause, its type's name, and its project's name, ladder, default
    label and hub sink's clearance (``member_channels.project_rules``)."""
    q, r, t, pl = tables.agent_questions, tables.runs, tables.project_agent_types, tables.plugins
    rules = member_channels.project_rules().subquery()
    return (
        select(
            q.c.id,
            q.c.plugin_id,
            pl.c.name.label("plugin"),
            q.c.project_id,
            q.c.ref,
            q.c.asker,
            q.c.cleared,
            q.c.outcome,
            q.c.answer,
            q.c.run_id,
            r.c.state.label("run_state"),
            r.c.failure_cause,
            t.c.name.label("agent_type"),
            rules.c.name.label("project"),
            rules.c.levels,
            rules.c.locations,
            rules.c.clearance,
            tables.projects.c.default_label,
        )
        .join_from(q, r, r.c.id == q.c.run_id)
        .join(t, t.c.id == q.c.agent_type_id)
        .join(pl, pl.c.id == q.c.plugin_id)
        .join(tables.projects, tables.projects.c.id == q.c.project_id)
        .join(rules, rules.c.name == tables.projects.c.name)
    )


def _cleared_now(found) -> bool:
    """Whether the project of question ``found`` is cleared now for the place its answer goes; fails closed."""
    cleared = found.cleared if isinstance(found.cleared, dict) else {}
    level = cleared.get("level")
    if not isinstance(level, str):
        return False
    return not member_channels.kept_from(found, level, cleared.get("location"), cleared.get("below") is True)


def _answer(found) -> dict:
    """What a plugin reads of question ``found`` now: its outcome, pending until its run ended, refused with no text
    when its project is kept from the place the answer goes now."""
    state = found.outcome or ("pending" if found.run_state not in question_runs.RESULT_STATES else found.run_state)
    text = found.answer if state == "done" else None
    if state != "pending" and not _cleared_now(found):
        state, text = "refused", None
    return {
        "question_id": found.id,
        "ref": found.ref,
        "project": found.project,
        "agent_type": found.agent_type,
        "run_id": found.run_id,
        "state": state,
        "text": text,
        "label": label_of(found.project, found.default_label),
        "failure_cause": found.failure_cause if state == "failed" else None,
    }


async def answer_of(conn: AsyncConnection, plugin: str, question_id: int) -> dict:
    """The answer of question ``question_id`` of core plugin ``plugin`` (``_answer``); 404, the same words, for a
    question of another plugin and for none. The plugin need not be enabled any more: the question is its own."""
    q = tables.agent_questions
    found = (await conn.execute(_question_select().where(q.c.id == question_id))).one_or_none()
    if found is None or found.plugin != plugin:
        raise HTTPException(404, NO_QUESTION.format(id=question_id))
    return _answer(found)


# At the claim, the report and the end


async def question_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of question run ``view`` (``runs.service.views.run_view``) as its worker claims it."""
    q = tables.agent_questions
    found = (await conn.execute(_question_select().where(q.c.run_id == view.id))).one()
    r = tables.runs
    profile_id = (await conn.execute(select(r.c.profile_id).where(r.c.id == view.id))).scalar_one()
    profile = await by_id(conn, profile_id)
    return question_runs.build_question_prompt(
        agent={
            "handle": profile.handle,
            "name": profile.name,
            "project": profile.project,
            "description": profile.description,
        },
        plugin=found.plugin,
        agent_type=found.agent_type,
        asker=found.asker,
        label=label_of(found.project, found.default_label),
        question=view.request or "",
        repos=[repo.model_dump() for repo in view.repos or []],
    )


async def keep_answer(conn: AsyncConnection, run_id: int, answer: str) -> None:
    """Keep ``answer``, of the report of done of question run ``run_id``, for the end of the run, which moves in the
    same transaction."""
    q = tables.agent_questions
    text = question_runs.answer_text(answer)
    await conn.execute(
        update(q).where(q.c.run_id == run_id).values(answer=text, outcome="done", answered_at=func.now())
    )


async def question_run_ended(conn: AsyncConnection, run_id: int, new: str) -> int:
    """Store the outcome of question run ``run_id``, moved to ``new``, when ``new`` ends it (RESULT_STATES), and the
    event agent.answer for the plugin of its agent type; how many deliveries. The project kept by now from the place
    the answer goes, the outcome is refused and no text is kept."""
    if new not in question_runs.RESULT_STATES:
        return 0
    q = tables.agent_questions
    found = (await conn.execute(_question_select().where(q.c.run_id == run_id))).one_or_none()
    if found is None:  # a question run of no question: none is made so
        log.warning("question run without a question", extra={"run_id": run_id})
        return 0
    outcome = new if _cleared_now(found) else "refused"
    text = found.answer if outcome == "done" else None
    label = label_of(found.project, found.default_label)
    values = {"outcome": outcome, "answer": text, "label": label, "answered_at": func.now()}
    await conn.execute(update(q).where(q.c.id == found.id).values(values))
    payload = {
        "question_id": found.id,
        "ref": found.ref,
        "project": found.project,
        "agent_type": found.agent_type,
        "run_id": run_id,
        "state": outcome,
        "text": text,
        "label": label,
        "failure_cause": found.failure_cause if outcome == "failed" else None,
    }
    return await plugin_events.emit(
        conn, question_runs.ANSWER_EVENT, found.project_id, payload, plugin_id=found.plugin_id
    )


async def expire_queued(engine: AsyncEngine, batch: int, ended: Counter) -> int:
    """Fail each question run still queued QUEUE_SECONDS after it was queued, by the reaper, with the cause no_worker;
    how many."""
    r, u = tables.runs, tables.users
    late = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, question_runs.QUEUE_SECONDS)
    query = (
        select(r.c.id, u.c.login)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.kind == "question", r.c.state == "queued", r.c.queued_at < late)
        .order_by(r.c.id)
        .limit(batch)
        .with_for_update(of=r, skip_locked=True)
    )
    count = 0
    async with engine.begin() as conn:
        for run_id, login in (await conn.execute(query)).all():
            error = NO_WORKER.format(seconds=question_runs.QUEUE_SECONDS, login=login)
            columns = {"failure_cause": "no_worker"}
            await move_run(conn, run_id, "queued", "failed", "reaper", reason=error, error=error, columns=columns)
            count += 1
    ended["failed"] += count
    if count:
        log.warning("question runs no worker claimed", extra={"runs": count})
    return count
