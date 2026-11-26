"""Decisions: the questions the agent of a plan run or an author run asks its owner, and the owner's answers.
``docs/notifications.md`` (Decisions) is the design, ``runs`` holds the categories, states and limits, and
``run_state`` moves the runs.

POST /v1/worker/runs/{id}/decisions takes a decision from the worker holding a plan run, an author run or a chat run
(404 for any other run: a run of one step, a review run, a judge run): a category of ``runs.DECISION_CATEGORIES`` (a
chat run's is ``triage`` alone, and ``triage`` is no other run's; 422 for any other, and for a question of a plan run
or an author run that asks for a credential's value in words, ``admit_ask``), a question, a context in markdown of at most 16 KiB, 2 to 6 options with unique keys, the key of the option the
agent recommends, and optionally why, whether the act it asks about can be undone and how, and the refs of what it
rests on (``decision_models``, which holds the models, 422 for a ref in no form ``decision_asks`` reads), the step it
is about, which must be a step of the run's plan (422 for an author run that has put no plan yet, which has no steps),
and the outputs of the run it carries (``output_ids``, 422 for one of another run;
``output_store``), which every view of the decision lists with the decision's URL of their bytes. The decision keeps the plan the run has when its agent asks, none (``plan_id`` null)
for an author run that has put no plan yet. It answers 201 with the decision, whose id the agent waits for, leaves a
``system`` event in the run's log and notifies the run's owner (``notifications.notify``); the notification of a
decision without a plan names the author run and its title instead. A run has at most MAX_OPEN_DECISIONS open at once
(409). A plan run or an author run whose autonomy card leaves merging (K5) or deploying and releasing (K6) to its
agent gets 409 for a question of category merge or deploy, or a scope question asking leave to do it, and so does a
question in the category of any other class its card has auto (``autonomy_card.category_left_to_agent``: run
#1675 asked under access to merge, at A5); the reason quotes the card's rule (``admit_ask``,
``autonomy_card.left_to_agent``); a run without a snapshot asks as before.

POST /v1/projects/{p}/decisions/{id}/answer is for the person it is asked of (the run's owner), one it was escalated
to, or an admin of the project or of the hub (``runs.service.admin_acts``), with a web session or a machine token of
their own: an admin's answer to another member's run writes an event into the run's log naming the admin and the
dispatcher, and tells the dispatcher with a notice; another member gets 403, as does a worker's token (the middleware keeps it under /v1/worker/) and the token
of any run that runs as its owner (``run_access``), and someone without a grant on the project 404. A
machine token also gets 403 when the run is on a worker set to take runs dispatched from the web only, held, pinned or
parked there (``runs.web_only_steering``): its answer would steer that worker's agent, or resume the parked run on it.
A decision that is no longer open gets 409. The answer names an option of the decision, gives text of the owner's own
(at most 4 KiB), or both. Under the plan's dispatch lock (for a decision with a plan) and the run's row lock, the
decision is answered, its notification read, and the answer goes to a run's inbox as a message that names the decision
(``run_inbox.decision_id``), with a ``user_message`` event, so the heartbeat counts it and the worker hands it to the
agent like any message of the owner's:

- a run that is queued or held (``runs.MESSAGE_STATES``, waiting included) gets it in its own inbox;
- a parked plan run is resumed: the hub queues a new plan run pinned to the parked run's worker, with
  ``resume_of_run_id`` naming it, its session, repos, model, timeout and the agent time used so far, at the plan's
  current revision; the parked run is ``done`` with the reason ``resumed as #N``, its decisions still open go to the
  new run, and the answer goes to the new run's inbox;
- a parked author run is resumed so only once the answer leaves none of its decisions open. Until then the answer waits
  in the parked run's inbox, and the run that resumes it (on the last answer, or on a reply in its chat) takes every
  message of that inbox the worker has not handed to an agent, so the agent reads them all in its first turn.

The audit row decision.answer names the decision, its run and the option, never the text. An answer through a
channel of a core plugin (``core_plugin_api.answer_decision``) adds ``via=<kind>`` to it, and the decision keeps that
kind in ``answered_via`` (schema 0259), which every view shows: null for an answer from the web or the CLI. Words with no
option are a free answer, and the web and ``evo-agents hub decision show`` say so with the channel they came through.

A credential request (a decision of category credential that carries ``credential_request``,
``evo_agents.hub.credential_requests``) is asked of the run's owner whoever the run's principal reports to, and takes
the hub's options. Its answer may carry the value of the variable it asks for (``AnswerIn.secret``):
``credential_answers`` keeps it as the owner's secret of kind env for the run's project and lets the run run as them
when the option says so, for the run's owner, before a parked run is resumed, so the run that resumes it is
leased both. An admin answering another member's request gives a value alone: it becomes the admin's own secret, which
a share for that run alone lends it (``credential_answers``), never the dispatcher's. The decision's answer text, and the inbox message, carry the hub's sentence of what was given, by name,
never the value. Before it is asked, the hub looks for the owner's env secret of that variable
(``credential_resolver``): the run leased one, or one the hub may bind under K12 auto, and the hub answers the request
itself, answered by nobody, with a notice to the owner; else the decision's context names the secret, and the option
``found`` binds it when the owner chooses it.

An option the agent marks ``waits`` says not yet (``worker ask --wait-option``): choosing it leaves the decision open
with ``held_at`` set and its notification read, and sends nothing to the agent, so the run goes on waiting, or parked,
for a later answer (``_hold``); words with such an option are 422, since no agent would read them. A decision of
category wait is a wait of the agent's (``run_waits``): the hub answers it when its time or its event comes, and the
person asked may answer it sooner like any other.

A decision of category release is the hub's own, asked of the owner of a release cycle that stopped, on the cycle's
last run (``release_cycles``): its answer names one of its options, which ``release_answer`` does (go on, close the
cycle, or pause the schedule) whatever the state of that run, and goes to no inbox. So is a decision of category loop,
asked of the person a work item of the Loop stands for once it failed a third time, on its last run (``loop.stall``):
``stall.answer_loop`` does the option chosen (run it again, drop it, leave it to a person).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy_card, autonomy_levels, credential_requests, ops_events, runs, tables
from evo_agents.hub.autonomy_levels import AUTO
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import (
    audit,
    credential_answers,
    credential_resolver,
    principals,
)
from evo_agents.hub.server import decision_resume as resume
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.decision_models import AnswerIn as AnswerIn
from evo_agents.hub.server.decision_models import Decision as Decision
from evo_agents.hub.server.decision_models import DecisionAsk as DecisionAsk
from evo_agents.hub.server.decision_models import DecisionList as DecisionList
from evo_agents.hub.server.decision_models import DecisionOption as DecisionOption
from evo_agents.hub.server.decision_models import OptionIn as OptionIn
from evo_agents.hub.server.decision_models import WakeOn as WakeOn
from evo_agents.hub.server.decision_reads import (
    DecisionId,
    asked_person,
    decision_view,
    readable_decision,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.member_channels import effective_label
from evo_agents.hub.server.notifications import decision_link, decision_settled, notify, one_line
from evo_agents.hub.server.output_store import link_decision
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import REFUSALS, RunId
from evo_agents.hub.server.runs.service import admin_acts
from evo_agents.hub.server.runs.service.controls import web_only_steering
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.sealing import Sealer
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_OPEN_DECISIONS = 20  # a run's agent asks at most this many before one is answered
QUESTION_IN_MESSAGE = 500  # characters of the question the answer's inbox message repeats

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["decisions"], responses={401: {"model": ErrorBody}})


# Asking


def _asked_step(run_id: int, plan, key: str | None) -> str | None:
    """The key the plan gives the step ``key`` names (an id or an order); 422 when the plan has no such step."""
    if key is None:
        return None
    try:
        index = step_index(plan, key)
    except (PlanProblem, TypeError) as exc:
        raise HTTPException(422, f"run {run_id}: {exc}; a decision is about a step of the run's plan") from None
    return step_key(plan["steps"][index], index)


async def _step_of_run(conn: AsyncConnection, run_id: int, row, key: str | None) -> str | None:
    """The step of its plan a decision of run ``run_id`` (``row``, as ``held_plan_run`` reads it) is about; 422 for a
    step its plan does not have, and for any step of an author run that has put no plan yet."""
    if row.plan_id is None:
        if key is not None:
            raise HTTPException(
                422,
                f"{row.kind} run {run_id} has put no plan on the hub yet, so it has no step {key!r}: leave out "
                "step_key, or put the plan first",
            )
        return None
    pl = tables.plans
    held = select(pl.c.body).where(pl.c.project_id == row.project_id, pl.c.plan_id == row.plan_id)
    current = (await conn.execute(held)).one_or_none()
    return _asked_step(run_id, current.body if current else {}, key)


def about_run(run_id: int, kind: str, title: str | None) -> str:
    """What a decision of run ``run_id`` is about when it has no plan: the author run and its title."""
    shown = f"{kind} run #{run_id}"
    return shown + (f", {one_line(title)}" if title else "")


async def _notify_asked(
    conn: AsyncConnection, row, run_id: int, decision_id: int, key: str | None, body, asked: int
) -> None:
    """Tell ``asked``, the person the decision just asked goes to, of it: what it is about, its plan and step, or for
    an author run without a plan, the run and its title, and who it is asked of."""
    r, u = tables.runs, tables.users
    title = (await conn.execute(select(r.c.title).where(r.c.id == run_id))).scalar_one()
    login = (await conn.execute(select(u.c.login).where(u.c.id == asked))).scalar_one()
    if row.plan_id is not None:
        about = f"plan {row.plan_id}" + (f", step {key}" if key else "")
    else:
        about = about_run(run_id, row.kind, title)
    who = "Run" if row.kind == "plan" else f"{row.kind.capitalize()} run"
    await notify(
        conn,
        user_id=asked,
        kind="decision",
        project_id=row.project_id,
        run_id=run_id,
        decision_id=decision_id,
        title=f"{who} #{run_id} asks: {one_line(body.question)}",
        body=f"{body.question.strip()}\n\n{body.category} decision for {about} in project {row.name}, asked of {login}.",
        link=decision_link(decision_id),
    )


async def store_decision(
    conn: AsyncConnection, run_id: int, row, body: DecisionAsk, *, wake: dict | None = None, quiet: bool = False
) -> Decision:
    """Store the decision ``body`` asks for run ``run_id``, whose row the caller holds locked, in the caller's
    transaction, with the outputs of the run it names, notify the run's owner and log it: ``row`` is what
    ``held_plan_run`` reads, or for a local run (``local_runs``) the same columns. 409 past MAX_OPEN_DECISIONS open,
    422 for a step the plan does not have or an output of another run. A wait (``run_waits``) gives ``wake``, its
    columns wake_at and wake_on, and notifies nobody: it asks nobody anything. So does a credential request the hub
    answers itself (``quiet``, ``_ask_credential``). A decision that asks someone writes ``decision.opened`` too
    (``ops_events``)."""
    d = tables.decisions
    opened = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    if (await conn.execute(opened)).scalar_one() >= MAX_OPEN_DECISIONS:
        raise HTTPException(
            409,
            f"run {run_id} has {MAX_OPEN_DECISIONS} decisions open already: wait for an answer before asking more",
        )
    key = await _step_of_run(conn, run_id, row, body.step_key)
    await checked_label(conn, run_id, body.label)
    # A credential request asks the run's owner for what is theirs alone to give: a value, their account.
    asked = row.dispatched_by if body.credential_request is not None else await principals.asked_of(conn, run_id)
    stored = (
        insert(d)
        .values(
            run_id=run_id,
            project_id=row.project_id,
            plan_id=row.plan_id,
            step_key=key,
            category=body.category,
            question=body.question,
            context=body.stored_context(),
            options=body.stored_options(),
            **body.stored_extras(),
            asked_of=asked,
            **(wake or {}),
        )
        .returning(d.c.id)
    )
    decision_id = (await conn.execute(stored)).scalar_one()
    await link_decision(conn, run_id, decision_id, body.output_ids)
    if wake is None and not quiet:
        await _notify_asked(conn, row, run_id, decision_id, key, body, asked)
        await ops_events.decision_opened(
            conn,
            project_id=row.project_id,
            decision_id=decision_id,
            category=body.category,
            run_id=run_id,
            asked_of=asked,
        )
    shown = f"decision #{decision_id} asked ({body.category}): {one_line(body.question)}"
    await write_event(
        conn, run_id, {"text": shown, "decision": {"id": decision_id, "category": body.category, "step": key}}
    )
    await _card_moved(conn, decision_id)
    return await decision_view(conn, decision_id)


async def checked_label(conn: AsyncConnection, run_id: int, label: str | None) -> None:
    """422 unless ``label``, the label a decision of run ``run_id`` asks with, is a level of the run's project's ladder
    at most the level the grant of the run's owner on the project reaches (the run's maximum); no label passes."""
    if label is None:
        return
    r, p, g = tables.runs, tables.projects, tables.grants
    query = (
        select(p.c.name, p.c.levels, g.c.max_level)
        .select_from(
            r.join(p, p.c.id == r.c.project_id).outerjoin(
                g, and_(g.c.project_id == r.c.project_id, g.c.user_id == r.c.dispatched_by)
            )
        )
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one()
    levels = list(row.levels or [])
    if label not in levels:
        raise HTTPException(
            422,
            f"run {run_id}: {label!r} is not a level of project {row.name}, whose levels are "
            f"{', '.join(levels) or 'none'}: label the decision with one of them, or leave the label out",
        )
    reach = levels.index(row.max_level) if row.max_level in levels else -1
    if levels.index(label) > reach:
        raise HTTPException(
            422,
            f"run {run_id}: the label {label} is above what the run reaches in project {row.name}, which is up to "
            f"{row.max_level or 'no level'}: label the decision at most that, or leave the label out",
        )


def _in_its_category(run_id: int, kind: str, category: str) -> None:
    """422 unless ``category`` is one the agent of a run of ``kind`` asks in: a chat run asks only
    ``runs.CHAT_DECISION_CATEGORY``, the other kinds those of ``runs.DECISION_CATEGORIES``."""
    if (kind == "chat") != (category == runs.CHAT_DECISION_CATEGORY):
        if kind == "chat":
            allowed = f"only {runs.CHAT_DECISION_CATEGORY!r}"
        else:
            allowed = ", ".join(runs.DECISION_CATEGORIES)
        raise HTTPException(
            422, f"run {run_id} is a {kind} run: its agent asks decisions of {allowed}, not {category!r}"
        )


# The kinds of run whose agent the hub holds to asking for a credential with a credential request, and to doing on its
# own what its autonomy card leaves to it: the agents that follow a plan or write one. A chat run asks triage alone, and a local run asks on a route of its own (local_runs).
CREDENTIAL_GATED_KINDS = ("plan", "author")


def admit_ask(run_id: int, kind: str, body: DecisionAsk, card: autonomy_card.Card | None = None) -> None:
    """The gate of a decision the agent of a run of ``kind`` asks, before the hub stores anything of it: 422 for a
    category the run does not ask in (``_in_its_category``), and for a question of a plan run or an author run that
    asks in words for a credential's value (``credential_requests.prose_credential``), whose reason names the command
    that asks it as a credential request; 409 for a question of such a run about a class its autonomy ``card`` leaves to
    the agent (``autonomy_card.left_to_agent``), whose reason quotes the card's rule. A refused question is kept
    nowhere: the reason repeats a variable's name or the card's line at most, and the log line says the run, the
    category and the class alone."""
    _in_its_category(run_id, kind, body.category)
    if kind not in CREDENTIAL_GATED_KINDS or body.credential_request is not None:
        return
    refusal = credential_requests.prose_credential(body.category, body.question, body.context)
    if refusal is not None:
        log.info("decision refused: credential in prose", extra={"run_id": run_id, "category": body.category})
        raise HTTPException(422, f"run {run_id}: {refusal}")
    left = autonomy_card.left_to_agent(card, body.category, body.question) or autonomy_card.category_left_to_agent(
        card, body.category
    )
    if left is not None:
        klass, rule = left
        log.info(
            "decision refused: autonomy leaves it to the agent",
            extra={"run_id": run_id, "category": body.category, "class": klass},
        )
        raise HTTPException(
            409,
            f"run {run_id}: your autonomy card leaves {klass} ({autonomy_levels.CLASS_NAMES[klass]}) to you, so the "
            f"hub takes no question of it; the card's rule: {rule!r}. Do it yourself without asking, and write the "
            'choice, why and how to undo it on a line that starts with "Decision:" in the evidence of the step.',
        )


async def _ask_credential(conn: AsyncConnection, user: Principal, run_id: int, row, body: DecisionAsk) -> Decision:
    """Store credential request ``body`` of run ``run_id`` after the hub looked for what its owner holds already
    (``credential_resolver``): answered by the hub when the run is leased the owner's secret for the variable, or once
    the hub bound it as K12 of the run's principal lets it; else asked of the owner, its context naming what the hub
    found and the command that binds it."""
    request = body.credential_request.model_dump()
    found = await credential_resolver.look(conn, user, run_id, request)
    if found is None or not found.any:
        return await store_decision(conn, run_id, row, body)
    choice = await credential_resolver.k12(conn, run_id)
    answers = credential_resolver.answerable(request) and (
        found.leased or (found.bindable and choice is not None and choice.mode == AUTO)
    )
    if not answers:
        if found.bindable:
            body.options = [OptionIn(**credential_resolver.found_option(found, leased=False)), *body.options]
        view = await store_decision(conn, run_id, row, body)
        context = credential_resolver.with_context(
            body.context, credential_resolver.inbox_context(found, view.id, None if choice is None else choice.mode)
        )
        d = tables.decisions
        await conn.execute(update(d).values(context=context).where(d.c.id == view.id))
        return await decision_view(conn, view.id)
    bound = not found.leased
    view = await store_decision(conn, run_id, row, body, quiet=True)
    if bound:
        name = await credential_resolver.bind_by_hub(conn, row, run_id, view.id, found, choice)
        found.leased = [name]
    option = credential_resolver.found_option(found, leased=True)
    body.options = [OptionIn(**option), *body.options]
    await _answer_by_hub(conn, run_id, row, view, body, found, option, bound=bound)
    return await decision_view(conn, view.id)


async def _answer_by_hub(conn: AsyncConnection, run_id: int, row, view: Decision, body, found, option, *, bound: bool):
    """Answer credential request ``view`` of run ``run_id`` as the hub, with ``option`` naming the owner's secret
    ``found`` leases to the run: answered by nobody, the answer in the run's inbox as an owner's would be, so the worker
    takes the run's leases again, a notice to the owner instead of a decision, and the audit row decision.answer
    ending by=hub."""
    d, i = tables.decisions, tables.run_inbox
    words = credential_resolver.answer_sentence(found, bound=bound)
    await conn.execute(
        update(d)
        .values(
            options=body.stored_options(),
            state="answered",
            answer_option=option["key"],
            answer_text=words,
            answered_at=func.now(),
        )
        .where(d.c.id == view.id)
    )
    text = answer_message(view.id, view.category, view.question, option, words)
    left = insert(i).values(run_id=run_id, sent_by=row.dispatched_by, body=text, decision_id=view.id).returning(i.c.id)
    message_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": "hub", "message_id": message_id, "decision_id": view.id}
    await write_user_message(conn, run_id, event)
    name = found.leased[0]
    await notify(
        conn,
        user_id=row.dispatched_by,
        kind="notice",
        notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
        project_id=row.project_id,
        run_id=run_id,
        details={"decision_id": view.id},
        title=f"Run #{run_id} got {found.env_var} from your secret {name}",
        body=f"{one_line(view.question)}\n\n{words}\n\nThe hub answered credential request #{view.id} itself: "
        "nothing waits on you. Delete or rebind the secret with `evo-agents hub secret` to change what runs get.",
        link=decision_link(view.id),
    )
    target = answer_target(row.name, row.plan_id, view.step_key, view.id, run_id, option["key"], row.kind) + " by=hub"
    await audit.record(
        conn, actor_id=None, token_id=None, action=audit.DECISION_ANSWER, target=target, project_id=row.project_id
    )
    log.info("credential request answered by the hub", extra={"run_id": run_id, "decision_id": view.id, "bound": bound})


@worker_router.post(
    "/runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: DecisionAsk, user: CurrentUser) -> Decision:
    """Ask the owner of a plan run, an author run or a chat run this worker holds a decision, and notify them."""
    async with request.app.state.engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RESUME_KINDS)
        card = await principals.card(conn, run_id) if row.kind in CREDENTIAL_GATED_KINDS else None
        admit_ask(run_id, row.kind, body, card)
        if body.credential_request is not None:
            view = await _ask_credential(conn, user, run_id, row, body)
        else:
            view = await store_decision(conn, run_id, row, body)
    log.info("decision asked", extra={"run_id": run_id, "decision_id": view.id, "category": body.category})
    return view


# Answering


def answer_message(decision_id: int, category: str, question: str, option: dict | None, text: str | None) -> str:
    """The inbox message that hands the owner's answer to the agent: the decision it answers, the option chosen and
    the owner's own words, within MAX_MESSAGE_BYTES."""
    lines = [f"Answer to decision #{decision_id} ({category}): {one_line(question, QUESTION_IN_MESSAGE)}"]
    if option is not None:
        lines.append(f"Chosen option: {option['key']}, {option['label']}.")
    if text is not None:
        lines.append(text.strip() if option is not None else f"The owner's answer:\n{text.strip()}")
    return runs.clip("\n".join(lines), runs.MAX_MESSAGE_BYTES)


def answer_target(
    project: str, plan_id: str | None, key: str | None, decision_id: int, run_id: int, option, kind: str = "author"
) -> str:
    step = "" if key is None else f"#{key}"
    where = f"{project}/{plan_id}{step}" if plan_id is not None else f"{project}/{kind}"
    return f"{where} decision:{decision_id} run:{run_id} option={option or '-'}"


async def _resume(conn: AsyncConnection, user: Principal, parked_id: int, decision_id: int, text: str) -> int:
    """Queue the run that resumes parked run ``parked_id``, in the caller's transaction under the plan's lock, as
    ``user`` answered decision ``decision_id`` with the inbox message ``text``; the new run's id (``resume_parked``)."""
    why = f"as {user.login} answered decision #{decision_id}"
    return await resume.resume_parked(conn, user, parked_id, why, woken_by=text)


@dataclass(frozen=True)
class Answered:
    """A decision just answered: as the routes show it, its run, and the run whose inbox took the answer."""

    decision: Decision
    run_id: int
    inbox_run: int


async def _asked_of(conn: AsyncConnection, user: Principal, access: ProjectAccess, decision_id: int):
    """(plan, step key, behalf) of decision ``decision_id``, once ``user`` may answer it: the person it is asked of,
    one it was escalated to, or an admin of the project (``admin_acts``), for whom ``behalf`` names the member who
    dispatched the run (None for anyone else); 403 for anyone else, and for a run's token."""
    if user.acts_for_run is not None:  # a run's token acts as the owner, but answers no decision (run_access)
        raise HTTPException(
            403,
            f"decision {decision_id} is a question to a member, and a run's token (evr_...) answers no decision of any "
            "run: the owner answers with their own sign-in",
        )
    d, r, u, owner = tables.decisions, tables.runs, tables.users, tables.users.alias("owner")
    query = (
        select(d.c.plan_id, d.c.step_key, u.c.id, u.c.login, r.c.dispatched_by, owner.c.login.label("owner"))
        .select_from(
            d.join(r, r.c.id == d.c.run_id)
            .join(u, u.c.id == asked_person())
            .join(owner, owner.c.id == r.c.dispatched_by)
        )
        .where(d.c.id == decision_id, d.c.project_id == access.project_id)
    )
    found = (await conn.execute(query)).one()
    if found.id == user.user_id or user.user_id in await principals.escalated_to(conn, decision_id):
        return found.plan_id, found.step_key, None
    held = admin_acts.standing(user, access)
    if held is None:
        who = "who dispatched its run" if found.id == found.dispatched_by else "whom it is asked of"
        raise HTTPException(
            403, f"only {found.login}, {who}, or an admin of project {access.name}, may answer decision {decision_id}"
        )
    return found.plan_id, found.step_key, admin_acts.OnBehalf(found.dispatched_by, found.owner, held, access.name)


def _chosen(decision_id: int, options: list[dict], key: str | None) -> dict | None:
    """The option of ``options`` with ``key``, None for no key; 422 for a key the decision lacks."""
    if key is None:
        return None
    chosen = next((option for option in options if option["key"] == key), None)
    if chosen is None:
        keys = ", ".join(option["key"] for option in options)
        raise HTTPException(422, f"decision {decision_id} has no option {key!r}; its options are {keys}")
    return chosen


def _takes_answer(decision_id: int, run_id: int, run_state: str, kind: str) -> None:
    """409 unless the run of the decision can take an answer: a local run that has not ended, or a run that is parked
    or holds messages."""
    if kind == "local":  # the session takes the answer at its next turn, whatever it does now, but not once it is over
        if run_state == "ended":
            raise HTTPException(
                409, f"run {run_id} of decision {decision_id} is a local run that ended: no session can take an answer"
            )
    elif run_state != "parked" and run_state not in runs.MESSAGE_STATES:
        raise HTTPException(409, f"run {run_id} of decision {decision_id} is {run_state}: no agent can take an answer")


async def _record_answer(
    conn: AsyncConnection,
    user: Principal,
    decision_id: int,
    option: str | None,
    words: str | None,
    via: str | None = None,
) -> None:
    """Mark the decision answered with ``option`` and ``words``, through the channel ``via`` (None from the web or the
    CLI), and its notification to ``user`` read."""
    d, n = tables.decisions, tables.notifications
    answered = {"answer_option": option, "answer_text": words, "answered_by": user.user_id, "answered_via": via}
    await conn.execute(
        update(d).values(state="answered", answered_at=func.now(), **answered).where(d.c.id == decision_id)
    )
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
    )
    await decision_settled(conn)
    await _card_moved(conn, decision_id)


async def _card_moved(conn: AsyncConnection, decision_id: int) -> None:
    """The work card of the trace of decision ``decision_id``'s run refreshed (``work_cards``)."""
    from evo_agents.hub.server.work_cards import decision_moved  # it hands work through modules that import this one

    await decision_moved(conn, decision_id)


async def _go_on_local(conn: AsyncConnection, run_id: int) -> None:
    """A local session that waited for answers goes on once none of its decisions is open."""
    d, r = tables.decisions, tables.runs
    still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    if (await conn.execute(still_open)).scalar_one() == 0:
        await conn.execute(update(r).values(state="running", waiting_since=None).where(r.c.id == run_id))


async def _to_inbox(conn: AsyncConnection, user: Principal, run_id: int, decision_id: int, text: str) -> None:
    """Leave the answer ``text`` in the inbox of run ``run_id``, with its ``user_message`` event."""
    i = tables.run_inbox
    left = insert(i).values(run_id=run_id, sent_by=user.user_id, body=text, decision_id=decision_id).returning(i.c.id)
    message_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": user.login, "message_id": message_id, "decision_id": decision_id}
    await write_user_message(conn, run_id, event)


async def answer_decision(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    decision_id: int,
    body: AnswerIn,
    *,
    via: str | None = None,
    sealer: Sealer | None = None,
) -> Answered:
    """Answer decision ``decision_id`` of ``project`` as ``user``, in the caller's transaction: the checks, the answer,
    its notification read, the inbox message, a parked run resumed and the audit row, as the module's docstring says;
    for a credential request, what the answer gives the run too (``credential_answers``), its value sealed with
    ``sealer``. The answer route and the channels of core plugins (``via="telegram"``, which the audit row names and the
    decision keeps in ``answered_via``; None from the route) both answer through it; it raises the HTTPException the
    route answers with."""
    access = await readable_decision(conn, user, project, decision_id, None)
    plan_id, key, behalf = await _asked_of(conn, user, access, decision_id)
    if via is not None and behalf is not None:  # a channel speaks for the person asked alone
        raise HTTPException(
            403,
            f"through a channel only the person decision {decision_id} is asked of answers it: an admin answers it on "
            "the hub",
        )
    check_dispatcher(access)
    # Answers of the plan's decisions, and its dispatches, one at a time: the run a decision belongs to stays put
    # until the transaction ends. The run's row before the decision's, as every move of a run takes them.
    run_id, run_state, kind = await resume.lock_run_of(conn, access.project_id, plan_id, decision_id)
    d = tables.decisions
    locked = select(d.c.state, d.c.category, d.c.question, d.c.options, d.c.credential_request)
    state, category, question, options, request = (
        await conn.execute(locked.where(d.c.id == decision_id).with_for_update())
    ).one()
    if state != "open":
        raise HTTPException(409, f"decision {decision_id} is {state}, not open: it takes no answer any more")
    asked = credential_answers.checked(decision_id, request, body)
    chosen = _chosen(decision_id, options, asked.option)
    if behalf is not None:  # an admin answers another member's run: its log and its dispatcher say so
        did = f"answered decision #{decision_id} of it" + ("" if asked.option is None else f" with {asked.option}")
        await admin_acts.record_act(conn, user, access, run_id, behalf, "answer", did, decision_id=decision_id)
    if category in runs.HUB_DECISION_CATEGORIES:
        return await _answer_hub(conn, user, project, decision_id, run_id, category, chosen, body, via)
    _takes_answer(decision_id, run_id, run_state, kind)
    await web_only_steering(conn, user, run_id, "answer its decisions", "answer on the web", "answered")
    if chosen is not None and chosen.get("waits") is True:
        target = answer_target(project, plan_id, key, decision_id, run_id, body.option, kind) + " held"
        return await _hold(conn, user, access.project_id, decision_id, run_id, chosen, body, target, via)
    words = body.text
    if credential_answers.acts(request, asked):  # before a resume, which copies what the run may do
        given = await credential_answers.give(
            conn, user, sealer, access, project, decision_id, run_id, request, asked, behalf=behalf
        )
        words = credential_answers.with_sentence(body.text, given)
    await _record_answer(conn, user, decision_id, asked.option, words, via)
    if kind == "local" and run_state == "waiting":  # the session waited for this: it goes on once none is open
        await _go_on_local(conn, run_id)
    resumes = await resume.resumes(conn, run_id, run_state, kind)
    text = answer_message(decision_id, category, question, chosen, words)
    inbox_run = await _resume(conn, user, run_id, decision_id, text) if resumes else run_id
    await _to_inbox(conn, user, inbox_run, decision_id, text)
    target = answer_target(project, plan_id, key, decision_id, run_id, asked.option, kind)
    if behalf is not None:
        target += f" for={behalf.owner} as={behalf.standing}"
    if inbox_run != run_id:
        target += f" resumed as run:{inbox_run}"
    target += await _label_said(conn, decision_id, via)
    if via is not None:
        target += f" via={via}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.DECISION_ANSWER,
        target=target,
        project_id=access.project_id,
    )
    return Answered(await decision_view(conn, decision_id), run_id, inbox_run)


async def _label_said(conn: AsyncConnection, decision_id: int, via: str | None) -> str:
    """What the audit row of an answer through a channel (``via``) adds: `` label=<level>``, the decision's effective
    label, which the answer carries; nothing for an answer from the web or the CLI."""
    if via is None:
        return ""
    d, p = tables.decisions, tables.projects
    query = select(d.c.label, p.c.default_label, p.c.levels).join_from(d, p, p.c.id == d.c.project_id)
    row = (await conn.execute(query.where(d.c.id == decision_id))).one()
    label = effective_label(row.label, row.default_label, row.levels)
    return "" if label is None else f" label={label}"


async def _hold(
    conn: AsyncConnection,
    user: Principal,
    project_id: int,
    decision_id: int,
    run_id: int,
    chosen: dict,
    body: AnswerIn,
    target: str,
    via: str | None,
) -> Answered:
    """The person asked chose ``chosen``, an option that waits: the decision stays open with ``held_at`` now, its
    notification is read, nothing goes to the agent and the run goes on waiting (or parked) for a later answer. The
    run's log and the audit row decision.answer (its target ending ``held``) say so. 422 with words of their own: words
    go to the agent, which an option that waits does not reach."""
    if body.text is not None:
        raise HTTPException(
            422,
            f"option {chosen['key']} of decision {decision_id} leaves it open for a later answer, and the agent hears "
            "nothing of it: send your words with another option, or alone",
        )
    d, n = tables.decisions, tables.notifications
    await conn.execute(update(d).values(held_at=func.now()).where(d.c.id == decision_id))
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
    )
    shown = (
        f"{user.login} chose {one_line(chosen['label'], 80)} on decision #{decision_id}: it stays open, and the run "
        "waits for a later answer"
    )
    await write_event(conn, run_id, {"text": shown, "decision": {"id": decision_id, "held": chosen["key"]}})
    if via is not None:
        target += f" via={via}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.DECISION_ANSWER,
        target=target,
        project_id=project_id,
    )
    return Answered(await decision_view(conn, decision_id), run_id, run_id)


async def _hub_does(conn: AsyncConnection, user: Principal, decision_id: int, category: str, option: str) -> None:
    """Do what the answer of a decision of the hub's own chose; a gate of autonomy reads the answer at its next pass."""
    if category == runs.AUTONOMY_DECISION_CATEGORY:
        return
    if category == runs.LOOP_DECISION_CATEGORY:
        from evo_agents.hub.server.loop.stall import (
            answer_loop,
        )  # it hands out runs through the routes that import this

        await answer_loop(conn, user, decision_id, option)
        return
    from evo_agents.hub.server.release_answer import answer_stop  # it queues runs through the routes that import this

    await answer_stop(conn, user, decision_id, option)


async def _answer_hub(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    decision_id: int,
    run_id: int,
    category: str,
    chosen: dict | None,
    body: AnswerIn,
    via: str | None,
) -> Answered:
    """Answer a decision of the hub's own (``runs.HUB_DECISION_CATEGORIES``): of a stopped release cycle, or of a work
    item of the Loop that failed a third time (see the module). The option chosen is done, the decision answered and
    its notification read, audited as decision.answer; no inbox takes it."""
    if chosen is None:
        d = tables.decisions
        options = (await conn.execute(select(d.c.options).where(d.c.id == decision_id))).scalar_one()
        keys = ", ".join(option["key"] for option in options)
        raise HTTPException(422, f"decision {decision_id} of the hub takes one of its options: {keys}")
    await _hub_does(conn, user, decision_id, category, chosen["key"])
    d, n = tables.decisions, tables.notifications
    answered = {
        "answer_option": body.option,
        "answer_text": body.text,
        "answered_by": user.user_id,
        "answered_via": via,
    }
    await conn.execute(
        update(d).values(state="answered", answered_at=func.now(), **answered).where(d.c.id == decision_id)
    )
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user.user_id, n.c.read_at.is_(None))
    )
    await decision_settled(conn)
    target = answer_target(project, None, None, decision_id, run_id, body.option, category)
    target += await _label_said(conn, decision_id, via)
    access = await project_access(conn, user, project)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.DECISION_ANSWER,
        target=target + (f" via={via}" if via is not None else ""),
        project_id=access.project_id,
    )
    return Answered(await decision_view(conn, decision_id), run_id, run_id)


@router.post(
    "/{project}/decisions/{decision_id}/answer",
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def answer(
    request: Request, project: ProjectName, decision_id: DecisionId, body: AnswerIn, user: CurrentUser
) -> Decision:
    """Answer a decision of a run one dispatched: the answer goes to the agent through the run's inbox, and a parked
    run is resumed on its worker in its session."""
    async with request.app.state.engine.begin() as conn:
        answered = await answer_decision(conn, user, project, decision_id, body, sealer=request.app.state.sealer)
    log.info(
        "decision answered",
        extra={
            "decision_id": decision_id,
            "run_id": answered.run_id,
            "inbox_run": answered.inbox_run,
            "login": user.login,
        },
    )
    return answered.decision
