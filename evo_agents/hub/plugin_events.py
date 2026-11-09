"""The events and notifications the hub delivers to plugins, at least once (``docs/plugins.md``, section Extension
points).

``emit`` stores, in the caller's transaction, one delivery of an event for each plugin enabled in the event's project
whose installed version takes it and whose grant there lets it read it: a run's state (``run.state``, with
``runs:read``), a message of a group chat (``chat.message``, with ``chats:read``), a step's status of a plan set or
changed (``plan.step``, with ``plans:read``; ``evo_agents.hub.plan_step_status``), the end of a task run
(``task.result``, with ``runs:dispatch``), which ``emit_task_result`` stores for the plugin of the run's agent type
alone (``evo_agents.hub.task_runs``), a rev of a claim of a Lean Canvas and an approved baseline of one
(``canvas.claim``, ``canvas.baseline``, with ``canvas:read``; the core plugin canvas emits them), a batch of signals
(``signal.posted``, with ``signals:read``; ``evo_agents.hub.signals``), an approved version of an SRS document
(``srs.version.approved``, with ``srs:read``; the core plugin srs emits it), and a task's status set or changed
(``tasks.status``, with ``tasks:read``; ``evo_agents.hub.task_status``), and the end of a question a core plugin asked
an agent type of its own (``agent.answer``, which ``emit`` stores for that plugin alone;
``evo_agents.hub.question_runs``), and what a group chat's bridge carries (``chat.bridge``, with ``chats:read``, to the
plugin of the bridge alone; ``evo_agents.hub.chat_bridge_events``). A core plugin takes the end of its own task runs and
questions without a grant, as it hands them without one (``task_dispatch.hand_own_task``,
``core_plugin_api.ask_agent_type``), and the events of its own (``OWN``) likewise: the image trusts it. An events
contribution takes the kinds its ``on`` names, or every kind when it names none. The same call writes an event of
``hub_events.KINDS`` (``plan.step`` and the five kinds from ``canvas.claim`` to ``tasks.status``) into the hub's own
table hub_events too, which the Loop reads by cursor and the wait of a plan run wakes on
(``evo_agents.hub.hub_events``), whether a plugin takes it or not. The table plugin_deliveries is the outbox: nothing
goes out while the transaction is open, and nothing is lost when the hub stops after the commit. ``emit_notification``
does the same for a notification of a project, to each isolated plugin's channel there, with ``notifications:send``: a
core plugin's channel is a kind of members' channel instead, which ``evo_agents.hub.server.notifications`` sends as it
sends Telegram's.

A payload carries ids and states, never a text the project's labels may keep from a plugin: a run's id, kind and
states, a message's ids, a step's id and statuses, a task's ids, state, branch and pull requests, a notification's
kind, member and link with the words a restricted project's Telegram message carries (``telegram.what_waits``). A
plugin that needs more asks the hub with its token, under its grant. ``agent.answer`` is the one exception, made on
purpose: it carries the answer's text, to the core plugin that asked alone, which the image trusts, once the hub
checked that the project is not kept from the place the answer goes (``core_plugin_api.restricted``); delivering it
checks that again (``_answer_payload``), and a project kept from it by then gets the state refused and no text.

``deliver`` is the job hub.deliver_plugin_events, every minute, queued too by a transaction that stores a delivery to
go out at once (``deliver_soon``: what a bridge carries, a notification) and by a pass for the soonest delivery it
leaves pending (``_next``), on a queue and a worker of its own (``evo_agents.hub.jobs``). It takes the pending
deliveries that are due and claims each in a short transaction of its own with ``FOR UPDATE SKIP LOCKED``: it checks
again that the plugin is installed, turned on, enabled in the project and granted what the kind needs, and fails the
delivery at once when it is not; else it moves the delivery's next_at CLAIM_SECONDS ahead and commits. Then, with no
transaction open and no row locked, it calls the plugin's contribution (``plugin_calls.call``): method ``event`` or
``channel`` of plugin/1, with the delivery's id, which stays the same on every try, and writes what came of it in
another short transaction, unless another pass took the delivery meanwhile. A delivery is delivered once the plugin
answered, so a hub that stops between the answer and the commit delivers it again once its claim runs out: at least
once. A failed try waits BACKOFF_SECONDS, doubling up to BACKOFF_MAX_SECONDS, and the MAX_ATTEMPTS-th fails it; a
channel that answers ``delivered: false`` fails it at once. A core plugin that asks for a later try, with an exception
carrying a number ``retry_after`` (``plugin_calls.retry_after_of``), gets it that many seconds later, within 1 second
and BACKOFF_MAX_SECONDS: the try counts in ``attempts`` and in ``put_off``, which fails the delivery at MAX_PUT_OFF, and
the tries that failed otherwise are ``attempts - put_off``. Deliveries that ended are kept KEEP_DAYS days.

A pass starts no call after PASS_SECONDS, and a plugin that took its whole TIMEOUT to answer, or whose host could not
be reached, gets no other call in the pass: the deliveries left wait for the next pass, untried, so one hung host holds
the worker for about one TIMEOUT a pass, never a batch of them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import access, hub_events, tables, telegram
from evo_agents.hub.plugin_calls import (
    CoreContext,
    PluginCallError,
    PluginHostClient,
    PluginHostUnavailable,
    asked_for,
    call,
    enabled_plugins,
    enablements,
)
from evo_agents.hub.question_runs import ANSWER_EVENT as ANSWER
from evo_agents.hub.telegram import what_waits
from evo_agents.plugins.protocol import EVENT_KINDS

log = logging.getLogger(__name__)

NOTIFICATION = "notification"
KINDS = (*EVENT_KINDS, NOTIFICATION)  # plugin_deliveries.kind (0038)
STATES = ("pending", "delivered", "failed")  # plugin_deliveries.state (0038)
PERMISSION = {
    "run.state": "runs:read",
    "chat.message": "chats:read",
    hub_events.PLAN_STEP: "plans:read",
    "task.result": "runs:dispatch",
    "agent.answer": "runs:dispatch",  # only a core plugin asks, and takes it ungranted
    "chat.bridge": "chats:read",  # to the core plugin of the bridge alone (evo_agents.hub.chat_bridge_events)
    hub_events.CLAIM: "canvas:read",
    hub_events.BASELINE: "canvas:read",
    hub_events.SIGNALS: "signals:read",
    hub_events.SRS_APPROVED: "srs:read",
    hub_events.SRS_USE_CASE_APPROVED: "srs:read",
    hub_events.TASK_STATUS: "tasks:read",
    NOTIFICATION: "notifications:send",
}
# the core plugin each of these kinds belongs to, which takes it ungranted: srs keeps the tasks of an approved version
# in step from its own event (``evo_agents.hub.server.srs.auto_tasks``)
OWN = {
    hub_events.SRS_APPROVED: "srs",
    hub_events.SRS_USE_CASE_APPROVED: "srs",
    hub_events.TASK_STATUS: "tasks",
}
MAX_ATTEMPTS = 8
MAX_PUT_OFF = 240  # tries a plugin may put off: a group chat at its pace for some minutes, a few seconds a try
BACKOFF_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
BATCH = 500
KEEP_DAYS = 14
TIMEOUT = 30.0
PASS_SECONDS = 40.0  # a pass starts no call after this: with TIMEOUT, it ends within the minute after the next
CLAIM_SECONDS = TIMEOUT + 60  # a claimed delivery is not due again for this long, unless its try is written first
MAX_ERROR_CHARS = 2000
REPUBLISH_SECONDS = 45  # a delivery the bus carried and nobody claimed since is published again (published_at, 0231)
BUS_GRACE_SECONDS = 120  # while the bus is on, the minute's pass takes only deliveries due this long


def subscriptions(manifest: dict, tier: str, kind: str) -> list[str]:
    """The names of the contributions of a manifest (as stored) that take ``kind``: its events contributions, or, for
    a notification, an isolated plugin's channels."""
    contributes = manifest.get("contributes") or {}
    if kind == NOTIFICATION:
        return [item["name"] for item in contributes.get("channels") or []] if tier == "isolated" else []
    return [item["name"] for item in contributes.get("events") or [] if kind in item.get("on", EVENT_KINDS)]


def _granted(manifest: dict, permissions, kind: str, tier: str | None) -> bool:
    """Whether a plugin of ``tier`` whose project grants it ``permissions`` reads events of ``kind``: a core plugin
    reads the end of its own task runs and questions (``task.result`` and ``agent.answer`` go to the plugin of the
    run's agent type alone) and the events of ``OWN`` that are its own ungranted."""
    if tier == "core" and (kind in ("task.result", ANSWER) or OWN.get(kind) == manifest.get("id")):
        return True
    needed = PERMISSION[kind]
    return needed in (permissions or []) and needed in asked_for(manifest)


async def _subscribers(conn: AsyncConnection, kind: str, project_id: int) -> list[tuple[int, str]]:
    found = []
    for row in await enabled_plugins(conn, project_id):
        if _granted(row.manifest, row.permissions, kind, row.tier):
            found += [(row.id, name) for name in subscriptions(row.manifest, row.tier, kind)]
    return found


async def emit(
    conn: AsyncConnection, kind: str, project_id: int | None, payload: dict, *, plugin_id: int | None = None
) -> int:
    """Store a delivery of the event ``kind`` of project ``project_id`` for each plugin that takes it (plugin
    ``plugin_id`` alone when given), in the caller's transaction; how many. An event of ``hub_events.KINDS`` is also
    written to hub_events, in the same transaction."""
    if project_id is None:
        return 0
    if kind in hub_events.KINDS:
        await hub_events.record(conn, kind, project_id, payload)
    found = [item for item in await _subscribers(conn, kind, project_id) if plugin_id in (None, item[0])]
    if found:
        values = [
            {"plugin_id": taker, "project_id": project_id, "kind": kind, "contribution": name, "payload": payload}
            for taker, name in found
        ]
        await conn.execute(insert(tables.plugin_deliveries), values)
    await deliver_soon(conn)  # a notification goes out once it commits
    return len(found)


async def emit_run_state(
    conn: AsyncConnection,
    run_id: int,
    row,
    old: str,
    new: str,
    actor: str,
    *,
    failure_cause: str | None = None,
    inferred: bool = False,
) -> int:
    """Store the ``run.state`` event of run ``run_id``, which ``actor`` moved from ``old`` to ``new``: ``row`` is what
    ``run_state.move_run`` got back from its UPDATE (project_id, kind, plan_id and step_key). A move to ``failed``
    adds ``failure_cause``, the run's cause when it has one, and ``failure_cause_inferred``, true when the hub guessed
    it from the error (plugin API 1.6); every other move has neither key."""
    change = {"run_id": run_id, "run_kind": row.kind, "from": old, "to": new, "actor": actor}
    payload = {**change, "plan_id": row.plan_id, "step": row.step_key}
    if new == "failed" and failure_cause:
        payload |= {"failure_cause": failure_cause, "failure_cause_inferred": inferred}
    return await emit(conn, "run.state", row.project_id, payload)


async def emit_task_result(conn: AsyncConnection, project_id: int, plugin_id: int, payload: dict) -> int:
    """Store the ``task.result`` event of a task run that ended, ``payload`` (``task_runs``), for plugin ``plugin_id``,
    the plugin of the run's agent type, alone."""
    return await emit(conn, "task.result", project_id, payload, plugin_id=plugin_id)


async def emit_notification(conn: AsyncConnection, notification_id: int, project_id: int | None) -> int:
    """Store a delivery of notification ``notification_id`` for each isolated plugin's channel in its project."""
    if project_id is None:
        return 0
    found = await _subscribers(conn, NOTIFICATION, project_id)
    if not found:
        return 0
    n, users, projects = tables.notifications, tables.users, tables.projects
    row = (
        await conn.execute(
            select(n, users.c.login, projects.c.name.label("project"))
            .join_from(n, users, users.c.id == n.c.user_id)
            .join(projects, projects.c.id == n.c.project_id)
            .where(n.c.id == notification_id)
        )
    ).one()
    payload = {
        "id": notification_id,
        "kind": row.kind,
        "notice_kind": row.notice_kind,
        "login": row.login,
        "project": row.project,
        "text": what_waits(row.kind, row.notice_kind),
        "link": row.link,
        "run_id": row.run_id,
        "decision_id": row.decision_id,
        "proposal_id": row.proposal_id,
    }
    values = [
        {
            "plugin_id": plugin_id,
            "project_id": project_id,
            "kind": NOTIFICATION,
            "contribution": name,
            "payload": payload,
        }
        for plugin_id, name in found
    ]
    await conn.execute(insert(tables.plugin_deliveries), values)
    await deliver_soon(conn)
    return len(found)


# Delivering


def backoff(attempts: int) -> timedelta:
    """How long a delivery waits after its ``attempts``-th failed try."""
    return timedelta(seconds=min(BACKOFF_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_MAX_SECONDS))


def _due(batch: int, grace: float = 0.0):
    d = tables.plugin_deliveries
    return (
        select(d.c.id)
        .where(d.c.state == "pending", d.c.next_at <= func.now() - timedelta(seconds=grace))
        .order_by(d.c.next_at, d.c.id)
        .limit(batch)
    )


def _lock_due(delivery_id: int):
    """Delivery ``delivery_id`` while it is pending and due, locked, with its plugin's installed version and what its
    project grants it now; no row when another pass holds it."""
    d, p, v, pp, projects = (
        tables.plugin_deliveries,
        tables.plugins,
        tables.plugin_versions,
        enablements(),  # project_plugins, and the features of the hub that are on (canvas, srs, tasks)
        tables.projects,
    )
    return (
        select(
            d.c.id,
            d.c.project_id,
            d.c.kind,
            d.c.contribution,
            d.c.payload,
            d.c.attempts,
            d.c.put_off,
            p.c.name,
            p.c.installed_version,
            p.c.disabled_reason,
            v.c.manifest,
            v.c.tier,
            pp.c.plugin_id.label("enabled"),
            pp.c.permissions,
            projects.c.name.label("project"),
        )
        .select_from(
            d.join(p, p.c.id == d.c.plugin_id)
            .join(projects, projects.c.id == d.c.project_id)
            .outerjoin(v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
            .outerjoin(pp, (pp.c.plugin_id == d.c.plugin_id) & (pp.c.project_id == d.c.project_id))
        )
        .where(d.c.id == delivery_id, d.c.state == "pending", d.c.next_at <= func.now())
        .with_for_update(of=d, skip_locked=True)
    )


def _set(delivery_id: int, **values):
    d = tables.plugin_deliveries
    return update(d).where(d.c.id == delivery_id).values(**values)


def _refusal(row) -> str | None:
    """Why the plugin of delivery ``row`` may not get it now, or None."""
    if row.installed_version is None:
        return f"plugin {row.name} is not installed any more"
    if row.disabled_reason:
        return f"plugin {row.name} is turned off: {row.disabled_reason}"
    if row.enabled is None:
        return f"project {row.project} does not enable plugin {row.name} any more"
    if not _granted(row.manifest, row.permissions, row.kind, row.tier):
        return f"plugin {row.name} is not granted {PERMISSION[row.kind]} in project {row.project}"
    if row.contribution not in subscriptions(row.manifest, row.tier, row.kind):
        return f"plugin {row.name} {row.installed_version} no longer takes {row.kind} in {row.contribution}"
    return None


def _params(row, payload: dict) -> tuple[str, dict]:
    if row.kind == NOTIFICATION:
        params = {"channel": row.contribution, "project": row.project, "notification": payload}
        return "channels", {**params, "delivery": row.id}
    params = {"event": row.contribution, "project": row.project, "payload": payload, "kind": row.kind}
    return "events", {**params, "delivery": row.id}


async def _answer_payload(conn: AsyncConnection, row) -> dict:
    """The payload of delivery ``row`` as it goes now: an ``agent.answer`` with a text, the state refused and no text
    when the project is kept now from the clearance its question named (``telegram.restricted_for``, failing closed:
    a question gone, or a clearance it cannot read, is kept)."""
    payload = row.payload
    if row.kind != ANSWER or not isinstance(payload, dict) or payload.get("text") is None:
        return payload
    q, p, ps = tables.agent_questions, tables.projects, tables.project_sinks
    hub_sink = (ps.c.project_id == p.c.id) & (ps.c.kind == access.HUB_KIND)
    query = (
        select(q.c.cleared, p.c.levels, p.c.locations, ps.c.clearance)
        .select_from(q.join(p, p.c.id == q.c.project_id).outerjoin(ps, hub_sink))
        .where(q.c.id == payload.get("question_id"), q.c.project_id == row.project_id)
    )
    found = (await conn.execute(query)).one_or_none()
    cleared = found.cleared if found is not None and isinstance(found.cleared, dict) else None
    if cleared is not None and isinstance(cleared.get("level"), str):
        level, location, below = cleared["level"], cleared.get("location"), cleared.get("below") is True
        if not telegram.restricted_for(found.clearance, found.levels, found.locations, level, location, below=below):
            return payload
    return {**payload, "state": "refused", "text": None}


@dataclass(frozen=True)
class _Claim:
    """A delivery claimed for one try: what the call needs, and its attempts and tries put off when claimed."""

    id: int
    name: str
    tier: str
    point: str
    item: dict
    params: dict
    attempts: int
    context: CoreContext
    put_off: int = 0


async def _claim(conn: AsyncConnection, delivery_id: int, engine, config, skip) -> _Claim | str | None:
    """Delivery ``delivery_id`` claimed for a try, or what came of it at once: "failed" when its plugin may not get it,
    "skipped" when ``skip(name, tier)`` says its plugin gets no call this pass, None when another pass holds it or it is
    not due any more."""
    row = (await conn.execute(_lock_due(delivery_id))).one_or_none()
    if row is None:
        return None
    refused = _refusal(row)
    if refused is not None:
        await conn.execute(_set(delivery_id, state="failed", attempts=row.attempts + 1, last_error=refused))
        log.info("plugin delivery refused", extra={"delivery_id": delivery_id, "why": refused})
        return "failed"
    if skip(row.name, row.tier):
        return "skipped"
    point, params = _params(row, await _answer_payload(conn, row))
    item = next(item for item in row.manifest["contributes"][point] if item["name"] == row.contribution)
    context = CoreContext(row.name, engine, config, row.project, row.project_id)
    await conn.execute(_set(delivery_id, next_at=func.now() + timedelta(seconds=CLAIM_SECONDS)))
    return _Claim(delivery_id, row.name, row.tier, point, item, params, row.attempts, context, row.put_off)


def put_off_for(retry_after: float) -> timedelta:
    """How long a delivery waits when its plugin asked for a try ``retry_after`` seconds later."""
    return timedelta(seconds=min(max(retry_after, 1.0), BACKOFF_MAX_SECONDS))


def _settle(claim: _Claim, result: dict | None, error: str | None, retry_after: float | None = None):
    """The outcome of ``claim``'s try and the statement that writes it, which changes nothing once another pass took
    the delivery (its attempts moved) or it ended. ``retry_after``: the seconds the plugin asked to wait, with its
    error."""
    d = tables.plugin_deliveries
    tried = claim.attempts + 1
    later = error is not None and retry_after is not None
    put_off = claim.put_off + 1 if later else claim.put_off
    failed = tried - put_off  # the tries that failed and were not put off
    if later and put_off >= MAX_PUT_OFF:
        outcome, values = "failed", {"state": "failed", "last_error": error}
    elif later:
        outcome, values = "retried", {"next_at": func.now() + put_off_for(retry_after), "last_error": error}
    elif error is not None and failed >= MAX_ATTEMPTS:
        outcome, values = "failed", {"state": "failed", "last_error": error}
    elif error is not None:
        outcome, values = "retried", {"next_at": func.now() + backoff(failed), "last_error": error}
    elif claim.point == "channels" and not result["delivered"]:
        said = f"plugin {claim.name}'s channel {claim.params['channel']} did not deliver it"
        outcome, values = "failed", {"state": "failed", "last_error": said}
    else:
        outcome, values = "delivered", {"state": "delivered", "delivered_at": func.now()}
    statement = (
        update(d)
        .where(d.c.id == claim.id, d.c.state == "pending", d.c.attempts == claim.attempts)
        .values(attempts=tried, put_off=put_off, **values)
    )
    return outcome, statement


@dataclass(frozen=True)
class _Tried:
    """What came of one try: the plugin's answer, or its error with the seconds it asked to wait (``retry_after``),
    and whether the plugin is silent: it took the whole TIMEOUT, or its host could not be reached."""

    result: dict | None
    error: str | None = None
    silent: bool = False
    retry_after: float | None = None


async def _try(claim: _Claim, host) -> _Tried:
    """The plugin's answer to ``claim``, or its error."""
    started = time.monotonic()
    try:
        result = await call(host, claim.name, claim.tier, claim.point, claim.item, claim.params, claim.context, TIMEOUT)
    except (PluginCallError, PluginHostUnavailable) as exc:
        error = " ".join(f"{type(exc).__name__}: {exc}".split())[:MAX_ERROR_CHARS]
        silent = isinstance(exc, PluginHostUnavailable) or time.monotonic() - started >= TIMEOUT
        return _Tried(None, error, silent, getattr(exc, "retry_after", None))
    return _Tried(result)


async def _deliver_one(engine: AsyncEngine, delivery_id: int, config, host, skip, silent: set) -> str | None:
    async with engine.begin() as conn:
        claim = await _claim(conn, delivery_id, engine, config, skip)
    if not isinstance(claim, _Claim):
        return claim
    tried = await _try(claim, host)  # no transaction open, no row locked
    if tried.silent:
        silent.add(claim.name if claim.tier == "core" else HOST)
        log.warning("plugin silent: no other call this pass", extra={"plugin": claim.name, "delivery_id": claim.id})
    outcome, statement = _settle(claim, tried.result, tried.error, tried.retry_after)
    async with engine.begin() as conn:
        if (await conn.execute(statement)).rowcount == 0:
            return None  # another pass took it after its claim ran out
    if outcome == "failed" and tried.error is not None:
        log.warning("plugin delivery failed", extra={"delivery_id": claim.id, "contribution": claim.name})
    return outcome


async def deliver_soon(conn: AsyncConnection) -> None:
    """Have what ``conn``'s transaction stored delivered once it commits: through the bus when it is on, else by
    queueing the job hub.deliver_plugin_events, which a job of it that waits already joins (its queueing lock)."""
    from evo_agents.hub import bus, jobs

    await bus.soon(jobs.DELIVER_PLUGIN_EVENTS, conn)


async def deliver_one(engine: AsyncEngine, delivery_id: int, *, config=None, host=None) -> str | None:
    """Delivery ``delivery_id`` claimed and tried once, as a pass does, for a message of the bus
    (``evo_agents.hub.deliverer``): what came of it, or None when it is not pending and due, or another holds it, which
    a message sent twice, or one for a delivery a pass took, comes to."""
    silent: set[str] = set()
    return await _deliver_one(engine, delivery_id, config, host, lambda name, tier: False, silent)


def publishable(batch: int = BATCH):
    """The deliveries due that the bus has not carried since they fell due, or carried more than REPUBLISH_SECONDS
    ago without anyone claiming them, marked published now, with their plugin's name and kind; for the delivery
    process, which publishes them once this commits (``evo_agents.hub.deliverer``)."""
    d, p = tables.plugin_deliveries, tables.plugins
    stale = func.now() - timedelta(seconds=REPUBLISH_SECONDS)
    unpublished = d.c.published_at.is_(None) | (d.c.published_at < d.c.next_at) | (d.c.published_at < stale)
    due = (
        select(d.c.id)
        .where(d.c.state == "pending", d.c.next_at <= func.now(), unpublished)
        .order_by(d.c.next_at, d.c.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return (
        update(d)
        .where(d.c.id.in_(due), p.c.id == d.c.plugin_id)
        .values(published_at=func.now())
        .returning(d.c.id, d.c.next_at, d.c.kind, p.c.name)
    )


def unpublish(ids: list[int]):
    """The statement that marks deliveries ``ids`` not carried, after the broker refused them."""
    d = tables.plugin_deliveries
    return update(d).where(d.c.id.in_(ids), d.c.state == "pending").values(published_at=None)


async def soonest(conn: AsyncConnection) -> datetime | None:
    """When the soonest pending delivery not due yet falls due, or None with none."""
    d = tables.plugin_deliveries
    found = select(func.min(d.c.next_at)).where(d.c.state == "pending", d.c.next_at > func.now())
    return (await conn.execute(found)).scalar_one_or_none()


async def _next(conn: AsyncConnection) -> datetime | None:
    """Queue a pass of hub.deliver_plugin_events for when the soonest pending delivery not due yet is, in ``conn``'s
    transaction, under a queueing lock of that second apart from ``deliver_soon``'s; that time, or None with none.
    A delivery due already was left by this pass (a silent plugin, PASS_SECONDS): the minute's pass takes it."""
    from evo_agents.hub import jobs

    when = await soonest(conn)
    if when is None:
        return None
    task = jobs.DELIVER_PLUGIN_EVENTS
    await jobs.JobQueue.in_transaction().defer(
        task, queueing_lock=jobs.at_lock(task, when), connection=conn, schedule_at=when
    )
    return when


HOST = ""  # in a pass's silent plugins: the plugin host, which every isolated plugin goes through


async def deliver(
    engine: AsyncEngine,
    *,
    config=None,
    host=None,
    batch: int = BATCH,
    schedule: bool = True,
    grace: float = 0.0,
) -> dict:
    """One pass of the job hub.deliver_plugin_events: each pending delivery that is due, claimed and tried once; how
    many were delivered, are to be tried again, failed, and were left for the next pass (skipped: a silent plugin, or
    the pass past PASS_SECONDS). Without ``host`` it opens a client to the plugin host of ``config``. With
    ``schedule``, it queues the pass of the soonest delivery it leaves pending (``_next``). With ``grace``, it takes
    only the deliveries due for that many seconds: the minute's pass while the bus is on, which leaves the bus what
    it carries."""
    report = {"delivered": 0, "retried": 0, "failed": 0, "skipped": 0}
    own = host is None and config is not None
    if own:
        host = PluginHostClient(config.plugin_host_url, config.plugin_host_token)
    silent: set[str] = set()

    def skip(name: str, tier: str) -> bool:
        return (name if tier == "core" else HOST) in silent

    deadline = time.monotonic() + PASS_SECONDS
    try:
        async with engine.begin() as conn:
            due = (await conn.execute(_due(batch, grace))).scalars().all()
        for number, delivery_id in enumerate(due):
            if time.monotonic() >= deadline:
                report["skipped"] += len(due) - number
                break
            outcome = await _deliver_one(engine, delivery_id, config, host, skip, silent)
            if outcome is not None:
                report[outcome] += 1
        async with engine.begin() as conn:
            d = tables.plugin_deliveries
            ended = (d.c.state != "pending") & (d.c.created_at < func.now() - timedelta(days=KEEP_DAYS))
            report["pruned"] = (await conn.execute(delete(d).where(ended))).rowcount
            if schedule:
                await _next(conn)
    finally:
        if own:
            await host.aclose()
    if report["retried"] or report["failed"] or report["skipped"]:
        log.warning("plugin deliveries", extra=report)
    elif report["delivered"]:
        log.info("plugin deliveries", extra=report)
    return report
