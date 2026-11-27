"""An agent's question: the gate it passes before the hub keeps anything of it (``admit_ask``), the decision stored with
its notification and its event (``store_decision``), and a credential request the hub may answer itself
(``_ask_credential``). ``ask`` is the worker's route."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import autonomy_card, autonomy_levels, credential_requests, ops_events, runs
from evo_agents.hub.autonomy_levels import AUTO
from evo_agents.hub.plans import PlanProblem, step_index, step_key
from evo_agents.hub.server import principals
from evo_agents.hub.server.base.errors import Conflict, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.credentials.service import resolver as credential_resolver
from evo_agents.hub.server.decisions import repository
from evo_agents.hub.server.decisions.models import Decision, DecisionAsk, OptionIn
from evo_agents.hub.server.decisions.service.answers import answer_by_hub, card_moved
from evo_agents.hub.server.decisions.service.reads import decision_view
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.outputs.service.store import link_decision
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.decisions")  # the name its log lines have always had

MAX_OPEN_DECISIONS = 20  # a run's agent asks at most this many before one is answered

# The kinds of run whose agent the hub holds to asking for a credential with a credential request, and to doing on its
# own what its autonomy card leaves to it: the agents that follow a plan or write one. A chat run asks triage alone, and a local run asks on a route of its own (local_runs).
CREDENTIAL_GATED_KINDS = ("plan", "author")


def _asked_step(run_id: int, plan, key: str | None) -> str | None:
    """The key the plan gives the step ``key`` names (an id or an order); 422 when the plan has no such step."""
    if key is None:
        return None
    try:
        index = step_index(plan, key)
    except (PlanProblem, TypeError) as exc:
        raise Unprocessable(f"run {run_id}: {exc}; a decision is about a step of the run's plan") from None
    return step_key(plan["steps"][index], index)


async def _step_of_run(conn: AsyncConnection, run_id: int, row, key: str | None) -> str | None:
    """The step of its plan a decision of run ``run_id`` (``row``, as ``held_plan_run`` reads it) is about; 422 for a
    step its plan does not have, and for any step of an author run that has put no plan yet."""
    if row.plan_id is None:
        if key is not None:
            raise Unprocessable(
                f"{row.kind} run {run_id} has put no plan on the hub yet, so it has no step {key!r}: leave out "
                "step_key, or put the plan first",
            )
        return None
    current = await repository.plan_of(conn, row.project_id, row.plan_id)
    return _asked_step(run_id, current.body if current else {}, key)


def about_run(run_id: int, kind: str, title: str | None) -> str:
    """What a decision of run ``run_id`` is about when it has no plan: the author run and its title."""
    shown = f"{kind} run #{run_id}"
    return shown + (f", {NOTIFIER.one_line(title)}" if title else "")


async def _notify_asked(
    conn: AsyncConnection, row, run_id: int, decision_id: int, key: str | None, body, asked: int
) -> None:
    """Tell ``asked``, the person the decision just asked goes to, of it: what it is about, its plan and step, or for
    an author run without a plan, the run and its title, and who it is asked of."""
    title = await repository.run_title(conn, run_id)
    login = await repository.login_of(conn, asked)
    if row.plan_id is not None:
        about = f"plan {row.plan_id}" + (f", step {key}" if key else "")
    else:
        about = about_run(run_id, row.kind, title)
    who = "Run" if row.kind == "plan" else f"{row.kind.capitalize()} run"
    await NOTIFIER.notify(
        conn,
        user_id=asked,
        kind="decision",
        project_id=row.project_id,
        run_id=run_id,
        decision_id=decision_id,
        title=f"{who} #{run_id} asks: {NOTIFIER.one_line(body.question)}",
        body=f"{body.question.strip()}\n\n{body.category} decision for {about} in project {row.name}, asked of {login}.",
        link=NOTIFIER.decision_link(decision_id),
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
    if await repository.open_count(conn, run_id) >= MAX_OPEN_DECISIONS:
        raise Conflict(
            f"run {run_id} has {MAX_OPEN_DECISIONS} decisions open already: wait for an answer before asking more",
        )
    key = await _step_of_run(conn, run_id, row, body.step_key)
    await checked_label(conn, run_id, body.label)
    # A credential request asks the run's owner for what is theirs alone to give: a value, their account.
    asked = row.dispatched_by if body.credential_request is not None else await principals.asked_of(conn, run_id)
    values = {
        "run_id": run_id,
        "project_id": row.project_id,
        "plan_id": row.plan_id,
        "step_key": key,
        "category": body.category,
        "question": body.question,
        "context": body.stored_context(),
        "options": body.stored_options(),
        **body.stored_extras(),
        "asked_of": asked,
        **(wake or {}),
    }
    decision_id = await repository.insert_decision(conn, values)
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
    shown = f"decision #{decision_id} asked ({body.category}): {NOTIFIER.one_line(body.question)}"
    await write_event(
        conn, run_id, {"text": shown, "decision": {"id": decision_id, "category": body.category, "step": key}}
    )
    await card_moved(conn, decision_id)
    return await decision_view(conn, decision_id)


async def checked_label(conn: AsyncConnection, run_id: int, label: str | None) -> None:
    """422 unless ``label``, the label a decision of run ``run_id`` asks with, is a level of the run's project's ladder
    at most the level the grant of the run's owner on the project reaches (the run's maximum); no label passes."""
    if label is None:
        return
    row = await repository.label_reach(conn, run_id)
    levels = list(row.levels or [])
    if label not in levels:
        raise Unprocessable(
            f"run {run_id}: {label!r} is not a level of project {row.name}, whose levels are "
            f"{', '.join(levels) or 'none'}: label the decision with one of them, or leave the label out",
        )
    reach = levels.index(row.max_level) if row.max_level in levels else -1
    if levels.index(label) > reach:
        raise Unprocessable(
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
        raise Unprocessable(f"run {run_id} is a {kind} run: its agent asks decisions of {allowed}, not {category!r}")


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
        raise Unprocessable(f"run {run_id}: {refusal}")
    left = autonomy_card.left_to_agent(card, body.category, body.question) or autonomy_card.category_left_to_agent(
        card, body.category
    )
    if left is not None:
        klass, rule = left
        log.info(
            "decision refused: autonomy leaves it to the agent",
            extra={"run_id": run_id, "category": body.category, "class": klass},
        )
        raise Conflict(
            f"run {run_id}: your autonomy card leaves {klass} ({autonomy_levels.CLASS_NAMES[klass]}) to you, so the "
            f"hub takes no question of it; the card's rule: {rule!r}. Do it yourself without asking, and write the "
            'choice, why and how to undo it on a line that starts with "Decision:" in the evidence of the step.',
        )


async def _ask_credential(conn: AsyncConnection, user: Principal, run_id: int, row, body: DecisionAsk) -> Decision:
    """Store credential request ``body`` of run ``run_id`` after the hub looked for what its owner holds already
    (``credential_resolver``): answered by the hub when the run is leased the owner's secret for the variable, or once
    the hub bound it as K12 of the run's principal lets it; else asked of the owner, its context naming what the hub
    found and the command that binds it."""
    assert body.credential_request is not None
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
        await repository.set_context(conn, view.id, context)
        return await decision_view(conn, view.id)
    bound = not found.leased
    view = await store_decision(conn, run_id, row, body, quiet=True)
    if bound:
        name = await credential_resolver.bind_by_hub(conn, row, run_id, view.id, found, choice)  # pyright: ignore[reportArgumentType]
        found.leased = [name]
    option = credential_resolver.found_option(found, leased=True)
    body.options = [OptionIn(**option), *body.options]
    await answer_by_hub(conn, run_id, row, view, body, found, option, bound=bound)
    return await decision_view(conn, view.id)


async def ask(engine: AsyncEngine, user: Principal, run_id: int, body: DecisionAsk) -> Decision:
    """Ask the owner of a plan run, an author run or a chat run the worker ``user`` holds a decision, in a transaction
    of its own, and notify them: 404 for any other run (``held_plan_run``)."""
    async with UnitOfWork(engine) as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RESUME_KINDS)
        card = await principals.card(conn, run_id) if row.kind in CREDENTIAL_GATED_KINDS else None
        admit_ask(run_id, row.kind, body, card)
        if body.credential_request is not None:
            view = await _ask_credential(conn, user, run_id, row, body)
        else:
            view = await store_decision(conn, run_id, row, body)
    log.info("decision asked", extra={"run_id": run_id, "decision_id": view.id, "category": body.category})
    return view
