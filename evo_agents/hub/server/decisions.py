"""Decisions: the questions a plan run's agent asks its owner, and the owner's answers. ``docs/notifications.md``
(Decisions) is the design, ``runs`` holds the categories, states and limits, and ``run_state`` moves the runs.

POST /v1/worker/runs/{id}/decisions takes a decision from the worker holding a plan run (404 for any other run, a run
of one step included): a category of ``runs.DECISION_CATEGORIES`` (422 for any other), a question, a context in
markdown of at most 16 KiB, 2 to 6 options with unique keys, the key of the option the agent recommends, and the step
it is about, which must be a step of the plan. It answers 201 with the decision, whose id the agent waits for, leaves
a ``system`` event in the run's log and notifies the run's owner (``notifications.notify``). A run has at most
MAX_OPEN_DECISIONS open at once (409).

POST /v1/projects/{p}/decisions/{id}/answer is for the run's owner alone, with a web session or a machine token of
their own: another member gets 403, as does a worker's token (the middleware keeps it under /v1/worker/), and someone
without a grant on the project 404. A decision that is no longer open gets 409. The answer names an option of the
decision, gives text of the owner's own (at most 4 KiB), or both. Under the plan's dispatch lock and the run's row
lock, the decision is answered, its notification read, and the answer goes to a run's inbox as a message that names
the decision (``run_inbox.decision_id``), with a ``user_message`` event, so the heartbeat counts it and the worker
hands it to the agent like any message of the owner's:

- a run that is queued or held (``runs.MESSAGE_STATES``, waiting included) gets it in its own inbox;
- a parked run is resumed: the hub queues a new plan run pinned to the parked run's worker, with
  ``resume_of_run_id`` naming it, its session, repos, model, timeout and the agent time used so far, at the plan's
  current revision; the parked run is ``done`` with the reason ``resumed as #N``, its decisions still open go to the
  new run, and the answer goes to the new run's inbox.

The audit row decision.answer names the decision, its run and the option, never the text.

GET /v1/projects/{p}/decisions lists a project's decisions, newest first, filtered by state, run and plan, a page at a
time, and GET .../decisions/{id} shows one, to the readers of the run's plan (``runs.visible_plans``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import runs
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import decision_link, notify, one_line
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import INSERT_USER_MESSAGE, NEXT_SEQ
from evo_agents.hub.server.run_state import move_run, notify_events, notify_queued, write_event
from evo_agents.hub.server.runs import (
    CURRENT_PLAN,
    LINE,
    MAX_ID,
    REFUSALS,
    RunId,
    _dispatcher,
    _held_plan_run,
    _lock_plan,
    visible_plans,
)
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_OPEN_DECISIONS = 20  # a run's agent asks at most this many before one is answered
LABEL_CHARS = 200  # an option's label, one line
DESCRIPTION_CHARS = 1000  # an option's description
MAX_OPTIONS_BYTES = 16 * 1024  # all the options of a decision as JSON, as schema 0010 bounds them
QUESTION_IN_MESSAGE = 500  # characters of the question the answer's inbox message repeats
MAX_LIST = 200
MAX_OFFSET = 100_000

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["decisions"], responses={401: {"model": ErrorBody}})

DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]
OptionKey = Annotated[str, Field(pattern=runs.OPTION_KEY, description="letters, digits, _ and -, at most 32")]


# Models


class OptionIn(BaseModel):
    key: OptionKey
    label: str = Field(min_length=1, max_length=LABEL_CHARS, pattern=LINE, description="one line")
    description: str | None = Field(None, min_length=1, max_length=DESCRIPTION_CHARS)


class DecisionAsk(BaseModel):
    category: Literal[runs.DECISION_CATEGORIES] = Field(description="the agent asks only about these")
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(
        None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES, description="markdown, at most 16 KiB of UTF-8"
    )
    options: list[OptionIn] = Field(min_length=runs.DECISION_OPTIONS[0], max_length=runs.DECISION_OPTIONS[1])
    recommended: OptionKey | None = Field(None, description="the key of the option the agent recommends")
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of the plan it is about"
    )

    @model_validator(mode="after")
    def _consistent(self):
        if not self.question.strip():
            raise ValueError("a decision needs a question")
        if self.context is not None and len(self.context.encode()) > runs.MAX_DECISION_CONTEXT_BYTES:
            raise ValueError(f"a decision's context is at most {runs.MAX_DECISION_CONTEXT_BYTES} bytes of UTF-8")
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("each option of a decision needs a key of its own")
        if self.recommended is not None and self.recommended not in keys:
            raise ValueError(f"the recommended option {self.recommended!r} is not one of {', '.join(keys)}")
        if len(json.dumps(self.stored_options(), ensure_ascii=False).encode()) > MAX_OPTIONS_BYTES:
            raise ValueError(f"the options of a decision take at most {MAX_OPTIONS_BYTES} bytes of JSON")
        return self

    def stored_options(self) -> list[dict]:
        stored = []
        for option in self.options:
            entry = {"key": option.key, "label": option.label}
            if option.description is not None:
                entry["description"] = option.description
            if option.key == self.recommended:
                entry["recommended"] = True
            stored.append(entry)
        return stored


class DecisionOption(BaseModel):
    key: str
    label: str
    description: str | None
    recommended: bool


class Decision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the plan run the decision belongs to")
    run_state: Literal[runs.RUN_STATES]
    plan_id: str
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.DECISION_CATEGORIES]
    question: str
    context: str | None = Field(description="markdown")
    options: list[DecisionOption]
    recommended: str | None = Field(description="the key of the option the agent recommends")
    state: Literal[runs.DECISION_STATES]
    owner: str = Field(description="the login of the run's owner, the one member who may answer")
    answer_option: str | None
    answer_text: str | None
    answered_by: str | None
    answer_run_id: int | None = Field(
        description="the run whose inbox the answer went to: the decision's run, or the run that resumed it"
    )
    asked_at: datetime
    answered_at: datetime | None
    delivered_at: datetime | None = Field(description="when the worker handed the answer to the agent")


class DecisionList(BaseModel):
    decisions: list[Decision] = Field(description="newest first")
    total: int = Field(description="decisions that match the filters")
    limit: int
    offset: int


class AnswerIn(BaseModel):
    option: OptionKey | None = Field(None, description="the key of one of the decision's options")
    text: str | None = Field(
        None, min_length=1, max_length=runs.MAX_ANSWER_BYTES, description="the owner's own words, at most 4 KiB"
    )

    @model_validator(mode="after")
    def _something(self):
        if self.text is not None and not self.text.strip():
            raise ValueError("an answer's text needs some words")
        if self.text is not None and len(self.text.encode()) > runs.MAX_ANSWER_BYTES:
            raise ValueError(f"an answer's text is at most {runs.MAX_ANSWER_BYTES} bytes of UTF-8")
        if self.option is None and self.text is None:
            raise ValueError("an answer names an option, gives text, or both")
        return self


# Reading decisions

DECISION_COLUMNS = """
SELECT d.id, p.name, d.run_id, r.state, d.plan_id, d.step_key, d.category, d.question, d.context, d.options, d.state,
       o.login, d.answer_option, d.answer_text, a.login,
       (SELECT i.run_id FROM run_inbox i WHERE i.decision_id = d.id ORDER BY i.id LIMIT 1),
       d.asked_at, d.answered_at, d.delivered_at
  FROM decisions d JOIN projects p ON p.id = d.project_id JOIN runs r ON r.id = d.run_id
  JOIN users o ON o.id = r.dispatched_by LEFT JOIN users a ON a.id = d.answered_by
"""
LIST_FILTERS = """
 WHERE d.project_id = %(project)s AND d.plan_id = ANY(%(plans)s)
   AND (cardinality(%(states)s::text[]) = 0 OR d.state = ANY(%(states)s))
   AND (%(run)s::bigint IS NULL OR d.run_id = %(run)s)
   AND (%(plan)s::text IS NULL OR d.plan_id = %(plan)s)
"""
LIST_PAGE = DECISION_COLUMNS + LIST_FILTERS + " ORDER BY d.id DESC LIMIT %(limit)s OFFSET %(offset)s"
LIST_TOTAL = "SELECT count(*) FROM decisions d" + LIST_FILTERS
ONE_DECISION = DECISION_COLUMNS + " WHERE d.id = %s"


def _decision(row) -> Decision:
    (
        decision_id,
        project,
        run_id,
        run_state,
        plan_id,
        key,
        category,
        question,
        context,
        options,
        state,
        owner,
        answer_option,
        answer_text,
        answered_by,
        answer_run_id,
        asked_at,
        answered_at,
        delivered_at,
    ) = row
    shown = [
        DecisionOption(
            key=option["key"],
            label=option["label"],
            description=option.get("description"),
            recommended=option.get("recommended") is True,
        )
        for option in options
    ]
    return Decision(
        id=decision_id,
        project=project,
        run_id=run_id,
        run_state=run_state,
        plan_id=plan_id,
        step_key=key,
        category=category,
        question=question,
        context=context,
        options=shown,
        recommended=next((option.key for option in shown if option.recommended), None),
        state=state,
        owner=owner,
        answer_option=answer_option,
        answer_text=answer_text,
        answered_by=answered_by,
        answer_run_id=answer_run_id,
        asked_at=asked_at,
        answered_at=answered_at,
        delivered_at=delivered_at,
    )


async def decision_view(conn, decision_id: int) -> Decision:
    row = await (await conn.execute(ONE_DECISION, (decision_id,))).fetchone()
    return _decision(row)


def _no_decision(project: str, decision_id: int) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no decision {decision_id}: see GET /v1/projects/{project}/decisions"
    )


DECISION_PLAN = "SELECT plan_id FROM decisions WHERE id = %s AND project_id = %s"


async def readable_decision(conn, user: Principal, project: str, decision_id: int, sink: str | None) -> ProjectAccess:
    """The caller's access to ``project`` when it may read decision ``decision_id`` of it: a grant on the project (404
    without, 403 for a hub admin without one) and the decision's plan visible to it (404 otherwise, as for none)."""
    access = await project_access(conn, user, project)
    plan_routes._reader(access)
    row = await (await conn.execute(DECISION_PLAN, (decision_id, access.project_id))).fetchone()
    if row is None or row[0] not in await visible_plans(conn, access, sink):
        raise _no_decision(project, decision_id)
    return access


@router.get(
    "/{project}/decisions",
    response_model=DecisionList,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def list_decisions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[Literal[runs.DECISION_STATES]], Query(description="any of these states; repeat it")] = [],  # noqa: B006
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    plan_id: Annotated[str | None, Query(pattern=plan_routes.PLAN_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> DecisionList:
    """The project's decisions, newest first, of the plans the caller may read."""
    params = {
        "states": list(dict.fromkeys(state)),
        "run": run_id,
        "plan": plan_id,
        "limit": limit,
        "offset": offset,
    }
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        params |= {"project": access.project_id, "plans": await visible_plans(conn, access, sink)}
        page = [_decision(row) for row in await (await conn.execute(LIST_PAGE, params)).fetchall()]
        total = (await (await conn.execute(LIST_TOTAL, params)).fetchone())[0]
    return DecisionList(decisions=page, total=total, limit=limit, offset=offset)


@router.get(
    "/{project}/decisions/{decision_id}",
    response_model=Decision,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_decision(
    request: Request,
    project: ProjectName,
    decision_id: DecisionId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Decision:
    async with request.app.state.pool.connection() as conn:
        await readable_decision(conn, user, project, decision_id, sink)
        return await decision_view(conn, decision_id)


# Asking


OPEN_COUNT = "SELECT count(*) FROM decisions WHERE run_id = %s AND state = 'open'"
INSERT_DECISION = """
INSERT INTO decisions (run_id, project_id, plan_id, step_key, category, question, context, options)
VALUES (%(run)s, %(project)s, %(plan)s, %(step)s, %(category)s, %(question)s, %(context)s, %(options)s)
RETURNING id
"""


def _asked_step(run_id: int, plan, key: str | None) -> str | None:
    """The key the plan gives the step ``key`` names (an id or an order); 422 when the plan has no such step."""
    if key is None:
        return None
    try:
        index = step_index(plan, key)
    except (PlanProblem, TypeError) as exc:
        raise HTTPException(422, f"run {run_id}: {exc}; a decision is about a step of the run's plan") from None
    return step_key(plan["steps"][index], index)


@worker_router.post(
    "/runs/{run_id}/decisions",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask(request: Request, run_id: RunId, body: DecisionAsk, user: CurrentUser) -> Decision:
    """Ask the owner of a plan run this worker holds a decision, and notify them."""
    async with request.app.state.pool.connection() as conn:
        _, row = await _held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, _ = row
        if (await (await conn.execute(OPEN_COUNT, (run_id,))).fetchone())[0] >= MAX_OPEN_DECISIONS:
            raise HTTPException(
                409,
                f"run {run_id} has {MAX_OPEN_DECISIONS} decisions open already: wait for an answer before asking more",
            )
        current = await (await conn.execute(CURRENT_PLAN, (project_id, plan_id))).fetchone()
        key = _asked_step(run_id, current[0] if current else {}, body.step_key)
        params = {
            "run": run_id,
            "project": project_id,
            "plan": plan_id,
            "step": key,
            "category": body.category,
            "question": body.question,
            "context": body.context,
            "options": Jsonb(body.stored_options()),
        }
        decision_id = (await (await conn.execute(INSERT_DECISION, params)).fetchone())[0]
        about = f"plan {plan_id}" + (f", step {key}" if key else "")
        await notify(
            conn,
            user_id=dispatcher_id,
            kind="decision",
            project_id=project_id,
            run_id=run_id,
            decision_id=decision_id,
            title=f"Run #{run_id} asks: {one_line(body.question)}",
            body=f"{body.question.strip()}\n\n{body.category} decision for {about} in project {project}.",
            link=decision_link(decision_id),
        )
        shown = f"decision #{decision_id} asked ({body.category}): {one_line(body.question)}"
        await write_event(
            conn, run_id, {"text": shown, "decision": {"id": decision_id, "category": body.category, "step": key}}
        )
        view = await decision_view(conn, decision_id)
    log.info("decision asked", extra={"run_id": run_id, "decision_id": decision_id, "category": body.category})
    return view


# Answering

ANSWERED_DECISION = """
SELECT d.run_id, d.plan_id, d.step_key, r.dispatched_by, u.login
  FROM decisions d JOIN runs r ON r.id = d.run_id JOIN users u ON u.id = r.dispatched_by
 WHERE d.id = %s AND d.project_id = %s
"""
DECISION_RUN = "SELECT run_id FROM decisions WHERE id = %s"
LOCK_RUN = "SELECT state FROM runs WHERE id = %s FOR UPDATE"
LOCK_DECISION = "SELECT run_id, state, category, question, options FROM decisions WHERE id = %s FOR UPDATE"
ANSWER = """
UPDATE decisions SET state = 'answered', answer_option = %(option)s, answer_text = %(text)s, answered_by = %(user)s,
       answered_at = now()
 WHERE id = %(id)s
"""
READ_ITS_NOTIFICATION = """
UPDATE notifications SET read_at = now() WHERE decision_id = %s AND user_id = %s AND read_at IS NULL
"""
NEXT_RUN_ID = "SELECT nextval(pg_get_serial_sequence('runs', 'id'))"
# The run that resumes a parked plan run: pinned to its worker, in its session, with the agent time it used, at the
# plan's current revision (the one it was dispatched from when the plan is gone).
RESUME_RUN = """
INSERT INTO runs (id, kind, project_id, plan_id, title, plan_revision, dispatched_by, pinned_worker_id,
                  requested_runtime, runtime, model, mode, approval, timeout_s, max_attempts, repos, resume_of_run_id,
                  run_seconds, session_id)
OVERRIDING SYSTEM VALUE
SELECT %(new)s, 'plan', r.project_id, r.plan_id, r.title, coalesce(pl.revision, r.plan_revision), r.dispatched_by,
       r.worker_id, r.runtime, r.runtime, r.model, r.mode, r.approval, r.timeout_s, r.max_attempts, r.repos, r.id,
       r.run_seconds, r.session_id
  FROM runs r LEFT JOIN plans pl ON pl.project_id = r.project_id AND pl.plan_id = r.plan_id
 WHERE r.id = %(parked)s
"""
HAND_OVER_DECISIONS = "UPDATE decisions SET run_id = %s WHERE run_id = %s AND state = 'open'"
INSERT_ANSWER = """
INSERT INTO run_inbox (run_id, sent_by, body, decision_id) VALUES (%s, %s, %s, %s) RETURNING id
"""


def answer_message(decision_id: int, category: str, question: str, option: dict | None, text: str | None) -> str:
    """The inbox message that hands the owner's answer to the agent: the decision it answers, the option chosen and
    the owner's own words, within MAX_MESSAGE_BYTES."""
    lines = [f"Answer to decision #{decision_id} ({category}): {one_line(question, QUESTION_IN_MESSAGE)}"]
    if option is not None:
        lines.append(f"Chosen option: {option['key']}, {option['label']}.")
    if text is not None:
        lines.append(text.strip() if option is not None else f"The owner's answer:\n{text.strip()}")
    return runs.clip("\n".join(lines), runs.MAX_MESSAGE_BYTES)


def _answer_target(project: str, plan_id: str, key: str | None, decision_id: int, run_id: int, option) -> str:
    step = "" if key is None else f"#{key}"
    return f"{project}/{plan_id}{step} decision:{decision_id} run:{run_id} option={option or '-'}"


async def _resume(conn, user: Principal, parked_id: int, decision_id: int) -> int:
    """Queue the run that resumes parked run ``parked_id``, in the caller's transaction under the plan's lock, and end
    the parked one done; the new run's id. The parked run's decisions still open go to the new run."""
    new_id = (await (await conn.execute(NEXT_RUN_ID)).fetchone())[0]
    # The parked run leaves the active states first: a plan has one active plan run at a time.
    await move_run(
        conn,
        parked_id,
        "parked",
        "done",
        "owner",
        reason=f"resumed as #{new_id}",
        token_id=user.token_id,
        decisions=None,
    )
    await conn.execute(RESUME_RUN, {"new": new_id, "parked": parked_id})
    await conn.execute(HAND_OVER_DECISIONS, (new_id, parked_id))
    shown = f"resumes run #{parked_id} in its session, as {user.login} answered decision #{decision_id}"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": parked_id})
    await notify_queued(conn, new_id)
    return new_id


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
    async with request.app.state.pool.connection() as conn:
        access = await readable_decision(conn, user, project, decision_id, None)
        found = await (await conn.execute(ANSWERED_DECISION, (decision_id, access.project_id))).fetchone()
        _, plan_id, key, owner_id, owner = found
        if owner_id != user.user_id:
            raise HTTPException(403, f"only {owner}, who dispatched its run, may answer decision {decision_id}")
        _dispatcher(access)
        # Answers of the plan's decisions, and its dispatches, one at a time: the run a decision belongs to stays put
        # until the transaction ends. The run's row before the decision's, as every move of a run takes them.
        await _lock_plan(conn, access.project_id, plan_id)
        run_id = (await (await conn.execute(DECISION_RUN, (decision_id,))).fetchone())[0]
        run_state = (await (await conn.execute(LOCK_RUN, (run_id,))).fetchone())[0]
        _, state, category, question, options = await (await conn.execute(LOCK_DECISION, (decision_id,))).fetchone()
        if state != "open":
            raise HTTPException(409, f"decision {decision_id} is {state}, not open: it takes no answer any more")
        chosen = None
        if body.option is not None:
            chosen = next((option for option in options if option["key"] == body.option), None)
            if chosen is None:
                keys = ", ".join(option["key"] for option in options)
                raise HTTPException(
                    422, f"decision {decision_id} has no option {body.option!r}; its options are {keys}"
                )
        if run_state != "parked" and run_state not in runs.MESSAGE_STATES:
            raise HTTPException(
                409, f"run {run_id} of decision {decision_id} is {run_state}: no agent can take an answer"
            )
        params = {"id": decision_id, "option": body.option, "text": body.text, "user": user.user_id}
        await conn.execute(ANSWER, params)
        await conn.execute(READ_ITS_NOTIFICATION, (decision_id, user.user_id))
        inbox_run = await _resume(conn, user, run_id, decision_id) if run_state == "parked" else run_id
        text = answer_message(decision_id, category, question, chosen, body.text)
        message_id = (
            await (await conn.execute(INSERT_ANSWER, (inbox_run, user.user_id, text, decision_id))).fetchone()
        )[0]
        seq = (await (await conn.execute(NEXT_SEQ, (inbox_run,))).fetchone())[0]
        event = {"text": text, "from": user.login, "message_id": message_id, "decision_id": decision_id}
        await conn.execute(INSERT_USER_MESSAGE, (inbox_run, seq, Jsonb(event)))
        await notify_events(conn, inbox_run)
        target = _answer_target(project, plan_id, key, decision_id, run_id, body.option)
        if inbox_run != run_id:
            target += f" resumed as run:{inbox_run}"
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.DECISION_ANSWER,
            target=target,
            project_id=access.project_id,
        )
        view = await decision_view(conn, decision_id)
    log.info(
        "decision answered",
        extra={"decision_id": decision_id, "run_id": run_id, "inbox_run": inbox_run, "login": user.login},
    )
    return view
