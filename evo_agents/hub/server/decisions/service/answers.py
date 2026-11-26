"""An answer to a decision: who may give it (``_asked_of``), what it does to the decision, its notification, the run's
inbox and a parked run (``answer_decision``), an option that waits (``_hold``), a decision of the hub's own
(``_answer_hub``), and a credential request the hub answers itself (``answer_by_hub``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.server import audit, credential_answers, credential_resolver, principals
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, Unprocessable
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.decisions import repository
from evo_agents.hub.server.decisions.models import AnswerIn, Decision
from evo_agents.hub.server.decisions.service import resume
from evo_agents.hub.server.decisions.service.reads import decision_view, readable_decision, visible
from evo_agents.hub.server.notifications.service.deliveries import decision_settled
from evo_agents.hub.server.notifications.service.member_channels import effective_label
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.service import admin_acts
from evo_agents.hub.server.runs.service.controls import web_only_steering
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher
from evo_agents.hub.server.sealing import Sealer
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.decisions")  # the name its log lines have always had

QUESTION_IN_MESSAGE = 500  # characters of the question the answer's inbox message repeats


def answer_message(decision_id: int, category: str, question: str, option: dict | None, text: str | None) -> str:
    """The inbox message that hands the owner's answer to the agent: the decision it answers, the option chosen and
    the owner's own words, within MAX_MESSAGE_BYTES."""
    lines = [f"Answer to decision #{decision_id} ({category}): {NOTIFIER.one_line(question, QUESTION_IN_MESSAGE)}"]
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


async def card_moved(conn: AsyncConnection, decision_id: int) -> None:
    """The work card of the trace of decision ``decision_id``'s run refreshed (``work_cards``)."""
    from evo_agents.hub.server.work_cards import decision_moved  # it hands work through modules that import this one

    await decision_moved(conn, decision_id)


async def answer_by_hub(conn: AsyncConnection, run_id: int, row, view: Decision, body, found, option, *, bound: bool):
    """Answer credential request ``view`` of run ``run_id`` as the hub, with ``option`` naming the owner's secret
    ``found`` leases to the run: answered by nobody, the answer in the run's inbox as an owner's would be, so the worker
    takes the run's leases again, a notice to the owner instead of a decision, and the audit row decision.answer
    ending by=hub."""
    words = credential_resolver.answer_sentence(found, bound=bound)
    stored = body.stored_options()
    await repository.answered(conn, view.id, option=option["key"], words=words, by=None, options=stored)
    text = answer_message(view.id, view.category, view.question, option, words)
    message_id = await repository.insert_inbox(conn, run_id, row.dispatched_by, text, view.id)
    event = {"text": text, "from": "hub", "message_id": message_id, "decision_id": view.id}
    await write_user_message(conn, run_id, event)
    name = found.leased[0]
    await NOTIFIER.notify(
        conn,
        user_id=row.dispatched_by,
        kind="notice",
        notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
        project_id=row.project_id,
        run_id=run_id,
        details={"decision_id": view.id},
        title=f"Run #{run_id} got {found.env_var} from your secret {name}",
        body=f"{NOTIFIER.one_line(view.question)}\n\n{words}\n\nThe hub answered credential request #{view.id} itself: "
        "nothing waits on you. Delete or rebind the secret with `evo-agents hub secret` to change what runs get.",
        link=NOTIFIER.decision_link(view.id),
    )
    target = answer_target(row.name, row.plan_id, view.step_key, view.id, run_id, option["key"], row.kind) + " by=hub"
    await Auditor(conn, row.project_id).record(audit.DECISION_ANSWER, target)
    log.info("credential request answered by the hub", extra={"run_id": run_id, "decision_id": view.id, "bound": bound})


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
        raise Forbidden(
            f"decision {decision_id} is a question to a member, and a run's token (evr_...) answers no decision of any "
            "run: the owner answers with their own sign-in",
        )
    found = await repository.asker(conn, access.project_id, decision_id)
    if found.id == user.user_id or user.user_id in await principals.escalated_to(conn, decision_id):
        return found.plan_id, found.step_key, None
    held = admin_acts.standing(user, access)
    if held is None:
        who = "who dispatched its run" if found.id == found.dispatched_by else "whom it is asked of"
        raise Forbidden(
            f"only {found.login}, {who}, or an admin of project {access.name}, may answer decision {decision_id}"
        )
    return found.plan_id, found.step_key, admin_acts.OnBehalf(found.dispatched_by, found.owner, held, access.name)


def _chosen(decision_id: int, options: list[dict], key: str | None) -> dict | None:
    """The option of ``options`` with ``key``, None for no key; 422 for a key the decision lacks."""
    if key is None:
        return None
    chosen = next((option for option in options if option["key"] == key), None)
    if chosen is None:
        keys = ", ".join(option["key"] for option in options)
        raise Unprocessable(f"decision {decision_id} has no option {key!r}; its options are {keys}")
    return chosen


def _takes_answer(decision_id: int, run_id: int, run_state: str, kind: str) -> None:
    """409 unless the run of the decision can take an answer: a local run that has not ended, or a run that is parked
    or holds messages."""
    if kind == "local":  # the session takes the answer at its next turn, whatever it does now, but not once it is over
        if run_state == "ended":
            raise Conflict(
                f"run {run_id} of decision {decision_id} is a local run that ended: no session can take an answer"
            )
    elif run_state != "parked" and run_state not in runs.MESSAGE_STATES:
        raise Conflict(f"run {run_id} of decision {decision_id} is {run_state}: no agent can take an answer")


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
    await repository.answered(conn, decision_id, option=option, words=words, by=user.user_id, via=via)
    await repository.read_notification(conn, decision_id, user.user_id)
    await decision_settled(conn)
    await card_moved(conn, decision_id)


async def _go_on_local(conn: AsyncConnection, run_id: int) -> None:
    """A local session that waited for answers goes on once none of its decisions is open."""
    if await repository.open_count(conn, run_id) == 0:
        await repository.go_on(conn, run_id)


async def _to_inbox(conn: AsyncConnection, user: Principal, run_id: int, decision_id: int, text: str) -> None:
    """Leave the answer ``text`` in the inbox of run ``run_id``, with its ``user_message`` event."""
    message_id = await repository.insert_inbox(conn, run_id, user.user_id, text, decision_id)
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
    its notification read, the inbox message, a parked run resumed and the audit row, as ``evo_agents.hub.server
    .decisions`` says; for a credential request, what the answer gives the run too (``credential_answers``), its value
    sealed with ``sealer``. The answer route and the channels of core plugins (``via="telegram"``, which the audit row
    names and the decision keeps in ``answered_via``; None from the route) both answer through it; it raises the
    HTTPException the route answers with."""
    access = await readable_decision(conn, user, project, decision_id, None)
    return await _answer(conn, user, access, decision_id, body, via, sealer)


async def answer(scope: Scope, decision_id: int, body: AnswerIn) -> Answered:
    """Answer decision ``decision_id`` of the scope's project from the route, once the caller may read it; the log
    line ``decision answered``."""
    await visible(scope.conn, scope.user, scope.access, decision_id, None)
    sealer = scope.request.app.state.sealer
    answered = await _answer(scope.conn, scope.user, scope.access, decision_id, body, None, sealer)
    log.info(
        "decision answered",
        extra={
            "decision_id": decision_id,
            "run_id": answered.run_id,
            "inbox_run": answered.inbox_run,
            "login": scope.user.login,
        },
    )
    return answered


async def _answer(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    decision_id: int,
    body: AnswerIn,
    via: str | None,
    sealer: Sealer | None,
) -> Answered:
    """What ``answer_decision`` does once the caller may read the decision, with its access to the project."""
    project = access.name
    plan_id, key, behalf = await _asked_of(conn, user, access, decision_id)
    if via is not None and behalf is not None:  # a channel speaks for the person asked alone
        raise Forbidden(
            f"through a channel only the person decision {decision_id} is asked of answers it: an admin answers it on "
            "the hub",
        )
    check_dispatcher(access)
    # Answers of the plan's decisions, and its dispatches, one at a time: the run a decision belongs to stays put
    # until the transaction ends. The run's row before the decision's, as every move of a run takes them.
    run_id, run_state, kind = await resume.lock_run_of(conn, access.project_id, plan_id, decision_id)
    state, category, question, options, request = await repository.locked(conn, decision_id)
    if state != "open":
        raise Conflict(f"decision {decision_id} is {state}, not open: it takes no answer any more")
    asked = credential_answers.checked(decision_id, request, body)
    chosen = _chosen(decision_id, options, asked.option)
    if behalf is not None:  # an admin answers another member's run: its log and its dispatcher say so
        did = f"answered decision #{decision_id} of it" + ("" if asked.option is None else f" with {asked.option}")
        await admin_acts.record_act(conn, user, access, run_id, behalf, "answer", did, decision_id=decision_id)
    if category in runs.HUB_DECISION_CATEGORIES:
        return await _answer_hub(conn, user, access, decision_id, run_id, category, chosen, body, via)
    _takes_answer(decision_id, run_id, run_state, kind)
    await web_only_steering(conn, user, run_id, "answer its decisions", "answer on the web", "answered")
    if chosen is not None and chosen.get("waits") is True:
        target = answer_target(project, plan_id, key, decision_id, run_id, body.option, kind) + " held"
        return await _hold(conn, user, access, decision_id, run_id, chosen, body, target, via)
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
    await Auditor.of(conn, user, access).record(audit.DECISION_ANSWER, target)
    return Answered(await decision_view(conn, decision_id), run_id, inbox_run)


async def _label_said(conn: AsyncConnection, decision_id: int, via: str | None) -> str:
    """What the audit row of an answer through a channel (``via``) adds: `` label=<level>``, the decision's effective
    label, which the answer carries; nothing for an answer from the web or the CLI."""
    if via is None:
        return ""
    row = await repository.label_of(conn, decision_id)
    label = effective_label(row.label, row.default_label, row.levels)
    return "" if label is None else f" label={label}"


async def _hold(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
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
        raise Unprocessable(
            f"option {chosen['key']} of decision {decision_id} leaves it open for a later answer, and the agent hears "
            "nothing of it: send your words with another option, or alone",
        )
    await repository.held(conn, decision_id)
    await repository.read_notification(conn, decision_id, user.user_id)
    shown = (
        f"{user.login} chose {NOTIFIER.one_line(chosen['label'], 80)} on decision #{decision_id}: it stays open, and the run "
        "waits for a later answer"
    )
    await write_event(conn, run_id, {"text": shown, "decision": {"id": decision_id, "held": chosen["key"]}})
    if via is not None:
        target += f" via={via}"
    await Auditor.of(conn, user, access).record(audit.DECISION_ANSWER, target)
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
    access: ProjectAccess,
    decision_id: int,
    run_id: int,
    category: str,
    chosen: dict | None,
    body: AnswerIn,
    via: str | None,
) -> Answered:
    """Answer a decision of the hub's own (``runs.HUB_DECISION_CATEGORIES``): of a stopped release cycle, or of a work
    item of the Loop that failed a third time (see ``evo_agents.hub.server.decisions``). The option chosen is done, the
    decision answered and its notification read, audited as decision.answer; no inbox takes it."""
    if chosen is None:
        options = await repository.options_of(conn, decision_id)
        keys = ", ".join(option["key"] for option in options)
        raise Unprocessable(f"decision {decision_id} of the hub takes one of its options: {keys}")
    await _hub_does(conn, user, decision_id, category, chosen["key"])
    await repository.answered(conn, decision_id, option=body.option, words=body.text, by=user.user_id, via=via)
    await repository.read_notification(conn, decision_id, user.user_id)
    await decision_settled(conn)
    target = answer_target(access.name, None, None, decision_id, run_id, body.option, category)
    target += await _label_said(conn, decision_id, via)
    await Auditor.of(conn, user, access).record(
        audit.DECISION_ANSWER, target + (f" via={via}" if via is not None else "")
    )
    return Answered(await decision_view(conn, decision_id), run_id, run_id)
