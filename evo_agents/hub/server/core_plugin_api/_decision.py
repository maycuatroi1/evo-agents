"""What a core plugin that sends a project's text out reads and writes (API 1.6): why a run failed, the open items of
the knowledge graph's review queue, what people corrected in their sessions, and the rules the project accepted, and,
in return, the cause of a failed run, a decision on the graph and the dismissal of a review item.

The plugin declares the sinks it sends text through in its manifest (key ``egress``, ``Plugin.egress``). Every text
read names one, or the plugin's only one, and returns only the items whose label the project's policy lets that sink
read: the sink is one the project declares in knowledge.yaml ``policy.sinks`` (and does not close), and its clearance
covers the item's label under ``Policy.allows``, the rule ``evo_agents.hub.judges.codex_cleared`` applies to Codex. An
item without a readable label counts as the highest of the project. A project without the sink gets an empty result
that says why, and a plugin that declares no egress is refused, for the writes too. Text is redacted
(``evo_agents.hub.redact``) and cut before it leaves. The review queue is read through the sink's own ceiling, as the
tools of the graph read through a sink: a node above its clearance is not there.

The writes act as the plugin, never as a member: a decision or a dismissal names the plugin as its actor
(migration 0174) and the audit row names it. A cause is changed only when the hub guessed it (a worker's cause is never
overwritten), the run's event says by which plugin, model and confidence and from which cause, and the cause stays
a guess. Writers undo each of them from the web and the CLI (``evo_agents.hub.server.kg_decisions``,
``kg_review`` and ``run_cause``). Private to ``core_plugin_api``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_decisions, runs, tables
from evo_agents.hub.access import ProjectRules
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import kg_root, queue_if_configured
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphCache, GraphOutdated, GraphUnavailable
from evo_agents.hub.kg_web import GraphProblem, WebSession, read
from evo_agents.hub.redact import redact
from evo_agents.hub.server import audit as audit_log
from evo_agents.hub.server.core_plugin_api._reads import enabled_project
from evo_agents.hub.server.core_plugin_api._types import EgressSink, Plugin, Refused, own_plugin
from evo_agents.hub.server.kg import GRAPHS_TRIED, built_graphs
from evo_agents.hub.server.run_cause import CAUSE_KEY, CLASSIFIED, classified
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.signals import project_rules
from evo_agents.kg.schema import known_relations

MAX_TEXT_CHARS = 2000  # a sanitized text of one item: an error, a turn, a summary, a snippet
MAX_EVENT_TAIL = 20  # the events of a run's log a failure's detail carries, the last ones
MAX_EVENT_CHARS = 400  # the text of one of them
MAX_TAIL_BYTES = 8 * 1024  # all of them together
MAX_READ_ITEMS = 500  # the most items one read returns
MAX_FACETS = 2000  # the sessions of the time range a read of corrections looks through
MAX_REASON_CHARS = kg_decisions.MAX_REASON_CHARS
NO_SINK = "project {project} declares no sink {sink} in its policy, or it clears no level its ladder knows"
NO_RUN = "project {project} has no run {run}"
NO_GRAPH = "project {project} has no graph on the hub yet"
GRAPH_CACHE = "decisions"  # the directory of the hub's kg data (``kg_root``) the graphs a plugin reads are fetched into

DECISION_KINDS = kg_decisions.KINDS


# What the reads return


@dataclass(frozen=True)
class Cleared:
    """What a text read returns: ``items``, those whose label ``sink`` may read, how many ``withheld`` the label (or the
    sink's absence) kept out, and ``reason`` when there are none to give: the project's policy declares no such sink,
    or the read found nothing at all."""

    sink: str
    items: tuple[Any, ...]
    withheld: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class RunEventText:
    """One event of a run's log, redacted and cut."""

    seq: int
    at: datetime
    kind: str
    text: str


@dataclass(frozen=True)
class RunFailure:
    """Why a run failed: its ``error`` redacted and cut, ``failure_cause`` (None while it has none), ``inferred``
    (true when the hub guessed the cause from the error, so a plugin may classify it), and the last of its events."""

    run_id: int
    project: str
    kind: str
    state: str
    error: str | None
    failure_cause: str | None
    inferred: bool
    events: tuple[RunEventText, ...]


@dataclass(frozen=True)
class ReviewNodeText:
    id: str
    kind: str
    name: str | None


@dataclass(frozen=True)
class ReviewItemText:
    """An open item of the review queue: its ``fingerprint`` (what a dismissal names), the ``rule`` that found it, what
    the rule suggests (``action``), the nodes it is about that the sink may read, and a ``snippet`` of their text."""

    fingerprint: str
    rule: str
    action: str
    nodes: tuple[ReviewNodeText, ...]
    snippet: str


@dataclass(frozen=True)
class CorrectionText:
    """A turn of a person that read as a correction: its ``text``, the ``evidence`` that names it
    (``session:<id>:user_turns:<n>``) and the time of its session."""

    text: str
    evidence: str
    at: datetime


@dataclass(frozen=True)
class AcceptedRule:
    """A proposal of the Curator the project accepted (``kind`` proposal), or its change once it merged (``change``)."""

    kind: str
    id: int
    title: str
    summary: str
    at: datetime | None


# What the writes return


@dataclass(frozen=True)
class CauseClassified:
    """The cause of a failed run the plugin set: the ``cause`` now, the ``previous`` one and the ``seq`` of the event
    of the run that says so. The cause is still a guess (``failure_cause_inferred``)."""

    run_id: int
    cause: str
    previous: str | None
    seq: int


@dataclass(frozen=True)
class DecisionTaken:
    """A decision on the graph the plugin took (``state`` active) or proposed (``proposed``), its ``number`` in the
    project and the project's decision ``rev`` it made."""

    number: int
    kind: str
    state: str
    rev: int


@dataclass(frozen=True)
class DismissedItem:
    """A review item the plugin dismissed, which stays dismissed in later builds that find it."""

    fingerprint: str
    rule: str


# The sink and the label


def _sink(plugin: Plugin, sink: str | None) -> EgressSink:
    """The sink of ``plugin`` that ``sink`` names, or its only one: ValueError when it has none, or the name is not one
    of its own."""
    own_plugin(plugin)
    if not plugin.egress:
        raise ValueError(f"plugin {plugin.id} declares no egress in its manifest, so it reads no project text")
    if sink is None:
        if len(plugin.egress) > 1:
            raise ValueError(f"plugin {plugin.id} declares several sinks: name one of {[s.id for s in plugin.egress]}")
        return plugin.egress[0]
    found = next((item for item in plugin.egress if item.id == sink), None)
    if found is None:
        raise ValueError(f"{sink!r} is no egress sink of plugin {plugin.id}")
    return found


def _declared(rules: ProjectRules, sink: str) -> bool:
    """Whether the project's policy declares ``sink`` at a clearance its ladder knows (``ceiling`` of the rules)."""
    return sink in rules.policy.sinks and sink not in getattr(rules, "_closed", set())


def _clears(rules: ProjectRules, label: Mapping | None, sink: str) -> bool:
    """Whether the project's policy lets ``sink`` read an item carrying ``label``, as stored: the very rule of
    ``judges.codex_cleared``, for the sink named instead of Codex's."""
    stored = rules.stored(label) if label is not None else rules.top()
    return stored is not None and rules.policy.allows(stored, sink)


def _text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    return runs.clip(redact(str(value)), limit)


async def _gate(conn: AsyncConnection, plugin: Plugin, project: str, sink: str | None):
    """(project id, the project's rules, the sink, why not): ``why`` is None when the project declares the sink."""
    chosen = _sink(plugin, sink)
    project_id = await enabled_project(conn, plugin, project)
    rules = await project_rules(conn, project_id)
    why = None if _declared(rules, chosen.id) else NO_SINK.format(project=project, sink=chosen.id)
    return project_id, rules, chosen.id, why


def _keep(rules: ProjectRules, sink: str, rows, limit: int, label_of: Callable, make: Callable) -> Cleared:
    items, withheld = [], 0
    for row in rows:
        if not _clears(rules, label_of(row), sink):
            withheld += 1
        elif len(items) < limit:
            items.append(make(row))
    reason = None if items else ("every item is above the sink's clearance" if withheld else "nothing to read")
    return Cleared(sink, tuple(items), withheld, reason)


def _limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_READ_ITEMS:
        raise ValueError(f"a read's limit is from 1 to {MAX_READ_ITEMS}")
    return limit


# Why a run failed


async def _inferred(conn: AsyncConnection, run_id: int) -> bool:
    e = tables.run_events
    guessed = e.c.body["failure_cause_inferred"].astext == "true"
    return bool(
        (await conn.execute(select(func.count()).where(e.c.run_id == run_id, e.c.kind == "system", guessed))).scalar()
    )


def _event_text(body: object) -> str:
    if isinstance(body, Mapping):
        for key in ("message", "text", "reason", "error", "line", "content"):
            if isinstance(body.get(key), str):
                return _text(body[key], MAX_EVENT_CHARS)
    return _text(json.dumps(body, ensure_ascii=False, default=str), MAX_EVENT_CHARS)


async def read_run_failure(
    conn: AsyncConnection, plugin: Plugin, project: str, run_id: int, *, sink: str | None = None
):
    """The detail of run ``run_id`` of ``project`` for a plugin that sends it out through ``sink`` (see the module): one
    ``RunFailure`` when the sink may read the run, with a sanitized error, the cause, whether the hub guessed it and the
    last events of its log within ``MAX_TAIL_BYTES``. A run is read at the label of its plan, or of the project's
    default when it has none. 404 for a run the project has not."""
    project_id, rules, chosen, why = await _gate(conn, plugin, project, sink)
    if why is not None:
        return Cleared(chosen, (), 0, why)
    r, p, e = tables.runs, tables.plans, tables.run_events
    row = (
        await conn.execute(
            select(
                r.c.id, r.c.kind, r.c.state, r.c.error, r.c.failure_cause, r.c.plan_id, p.c.label.label("plan_label")
            )
            .select_from(r.outerjoin(p, (p.c.project_id == r.c.project_id) & (p.c.plan_id == r.c.plan_id)))
            .where(r.c.id == run_id, r.c.project_id == project_id)
        )
    ).one_or_none()
    if row is None:
        raise Refused(404, NO_RUN.format(project=project, run=run_id))
    label = row.plan_label if row.plan_id is not None and row.plan_label is not None else rules.default_label
    if not _clears(rules, label, chosen):
        return Cleared(chosen, (), 1, "the run's label is above the sink's clearance")
    tail = (
        await conn.execute(
            select(e.c.seq, e.c.at, e.c.kind, e.c.body)
            .where(e.c.run_id == run_id)
            .order_by(e.c.seq.desc())
            .limit(MAX_EVENT_TAIL)
        )
    ).all()
    events, used = [], 0
    for item in tail:  # the newest first, so the cap keeps the last events
        text = _event_text(item.body)
        used += len(text.encode())
        if used > MAX_TAIL_BYTES:
            break
        events.append(RunEventText(item.seq, item.at, item.kind, text))
    failure = RunFailure(
        run_id=row.id,
        project=project,
        kind=row.kind,
        state=row.state,
        error=None if row.error is None else _text(row.error),
        failure_cause=row.failure_cause,
        inferred=await _inferred(conn, run_id),
        events=tuple(reversed(events)),
    )
    return Cleared(chosen, (failure,))


async def classify_run_failure(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    run_id: int,
    cause: str,
    *,
    model: str,
    confidence: float,
) -> CauseClassified:
    """Give failed run ``run_id`` of ``project`` the cause ``cause`` (one of ``runs.FAILURE_CAUSES``), which the hub had
    guessed from its error. 409 for a run that did not fail, that has a cause its worker sent (never overwritten), or
    whose cause the plugin set already. The cause stays a guess (``failure_cause_inferred`` of the run's events), so
    the rules that rerun a step read it as before; the run's log gets a ``system`` event naming the plugin, the
    ``model``, the ``confidence`` (0 to 1) and the previous cause, which a writer restores from the web or the CLI, and
    the audit log gets ``run.cause``."""
    if not plugin.egress:
        own_plugin(plugin)
        raise ValueError(f"plugin {plugin.id} declares no egress in its manifest, so it writes no decision")
    project_id = await enabled_project(conn, plugin, project)
    if cause not in runs.FAILURE_CAUSES:
        raise ValueError(f"a cause is one of {', '.join(runs.FAILURE_CAUSES)}")
    if not isinstance(model, str) or not 1 <= len(model) <= 100:
        raise ValueError("a model is a name of 1 to 100 characters")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("a confidence is a number from 0 to 1")
    r = tables.runs
    row = (
        await conn.execute(
            select(r.c.state, r.c.failure_cause).where(r.c.id == run_id, r.c.project_id == project_id).with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise Refused(404, NO_RUN.format(project=project, run=run_id))
    if row.state != "failed":
        raise Refused(409, f"run {run_id} is {row.state}: only a run that failed has a cause to classify")
    if not await _inferred(conn, run_id):
        raise Refused(409, f"the cause of run {run_id} came from its worker: the hub keeps it")
    if await classified(conn, run_id) is not None:
        raise Refused(409, f"the cause of run {run_id} was classified already: a writer restores it first")
    await conn.execute(r.update().values(failure_cause=cause).where(r.c.id == run_id))
    seq = await write_event(
        conn,
        run_id,
        {
            "message": f"Plugin {plugin.id} classified the cause as {cause} (model {model}, confidence "
            f"{round(float(confidence), 3)}); the hub had guessed {row.failure_cause}.",
            "failure_cause": cause,
            "failure_cause_inferred": True,
            CAUSE_KEY: {
                "step": CLASSIFIED,
                "plugin": plugin.id,
                "model": model,
                "confidence": round(float(confidence), 3),
                "previous_cause": row.failure_cause,
            },
        },
    )
    await audit_log.record(
        conn,
        actor_id=None,
        token_id=None,
        action=audit_log.RUN_CAUSE,
        target=f"{project} run:{run_id} cause={cause} was={row.failure_cause} plugin={plugin.id} model={model}",
        project_id=project_id,
    )
    return CauseClassified(run_id, cause, row.failure_cause, seq)


# What people corrected, and what the project accepted


async def read_corrections(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    *,
    since: datetime,
    until: datetime | None = None,
    sink: str | None = None,
    limit: int = 200,
) -> Cleared:
    """The turns of people that read as a correction (``review.is_correction``) in the sessions of ``project`` from
    ``since`` up to ``until``, newest session first, as the sessions' facets hold them: each with its redacted text,
    the evidence ``session:<id>:user_turns:<n>`` and the time of its session. Only sessions whose label the sink may
    read are looked at (see the module)."""
    limit = _limit(limit)
    if not isinstance(since, datetime) or (until is not None and not isinstance(until, datetime)):
        raise ValueError("since and until are datetimes")
    project_id, rules, chosen, why = await _gate(conn, plugin, project, sink)
    if why is not None:
        return Cleared(chosen, (), 0, why)
    f = tables.session_facets
    query = select(f.c.session_id, f.c.at, f.c.label, f.c.body).where(
        f.c.project_id == project_id, f.c.kind == "session", f.c.at >= since, f.c.body.is_not(None)
    )
    if until is not None:
        query = query.where(f.c.at < until)
    rows = (await conn.execute(query.order_by(f.c.at.desc(), f.c.id.desc()).limit(MAX_FACETS))).all()
    items, withheld = [], 0
    for row in rows:
        body = row.body if isinstance(row.body, dict) else {}
        turns = body.get("user_turns") if isinstance(body.get("user_turns"), list) else []
        indexes = [i for i in body.get("corrections") or [] if isinstance(i, int) and 0 <= i < len(turns)]
        if not indexes:
            continue
        if not _clears(rules, row.label, chosen):
            withheld += len(indexes)
            continue
        for index in indexes:
            if isinstance(turns[index], str) and len(items) < limit:
                items.append(
                    CorrectionText(_text(turns[index]), f"session:{row.session_id}:user_turns:{index}", row.at)
                )
    reason = None if items else ("every correction is above the sink's clearance" if withheld else "nothing to read")
    return Cleared(chosen, tuple(items), withheld, reason)


async def read_accepted_rules(
    conn: AsyncConnection, plugin: Plugin, project: str, *, sink: str | None = None, limit: int = 100
) -> Cleared:
    """The rules ``project`` accepted, newest first: each proposal of the Curator its owner accepted, as its change
    once that merged, with its title and summary. Each carries the label of its proposal (see the module)."""
    limit = _limit(limit)
    project_id, rules, chosen, why = await _gate(conn, plugin, project, sink)
    if why is not None:
        return Cleared(chosen, (), 0, why)
    p, c = tables.proposals, tables.curator_changes
    merged = c.c.merged_at.is_not(None)
    rows = (
        await conn.execute(
            select(
                p.c.id.label("proposal_id"),
                p.c.title,
                p.c.summary,
                p.c.label,
                p.c.answered_at,
                c.c.id.label("change_id"),
                c.c.merged_at,
            )
            .select_from(p.outerjoin(c, (c.c.proposal_id == p.c.id) & merged))
            .where(p.c.project_id == project_id, or_(p.c.state == "accepted", c.c.id.is_not(None)))
            .order_by(func.coalesce(c.c.merged_at, p.c.answered_at).desc().nulls_last(), p.c.id.desc())
            .limit(MAX_READ_ITEMS * 2)
        )
    ).all()

    def make(row) -> AcceptedRule:
        kind, ident, at = (
            ("change", row.change_id, row.merged_at)
            if row.change_id
            else ("proposal", row.proposal_id, row.answered_at)
        )
        return AcceptedRule(kind, ident, _text(row.title, 300), _text(row.summary), at)

    return _keep(rules, chosen, rows, limit, lambda row: row.label, make)


# The graph's review queue and decisions


async def _graph(
    conn: AsyncConnection, config: HubConfig, project_id: int, project: str, rules: ProjectRules, sink: str, reader
):
    """``reader(session)`` over the project's newest graph read through ``sink``'s ceiling, and its build id; (None, None)
    when no build succeeded yet."""
    graphs = await built_graphs(conn, project_id, GRAPHS_TRIED)
    if not graphs:
        return None, None
    ceiling = rules.ceiling(rules.levels[-1], sink)
    blobs = BlobStore.from_config(config)
    cache = GraphCache(kg_root(config.data_dir) / GRAPH_CACHE, keep=1)
    try:
        data, graph = await asyncio.to_thread(read, cache, blobs, project, rules.policy, ceiling, graphs, reader)
    except (GraphOutdated, GraphUnavailable) as exc:
        raise Refused(503, str(exc)) from None
    except ArtifactMismatch as exc:
        raise Refused(502, str(exc)) from None
    except GraphProblem as exc:
        raise Refused(503, f"the graph of project {project} cannot be read: {exc}") from None
    finally:
        if blobs is not None:
            blobs.close()
    return data, graph.build_id


def _queue(session: WebSession) -> dict:
    data = session.review()
    if not data.get("items"):
        return data
    store, b = session._visible_store()
    ids = sorted({node["id"] for item in data["items"] for node in item["nodes"]})
    return {**data, "props": {nid: n["props"] for nid, n in store.nodes(ids, b).items()}}


def _snippet(item: dict, props: Mapping[str, Mapping]) -> str:
    detail = item["detail"]
    samples = [s for s in detail.get("samples") or [] if isinstance(s, str)]
    if samples:
        return _text("; ".join(samples[:3]), 400)
    for node in item["nodes"]:
        found = props.get(node["id"]) or {}
        for key in ("text", "summary", "description", "path"):
            if isinstance(found.get(key), str) and found[key]:
                return _text(found[key], 400)
    return _text(", ".join(str(node["name"] or node["id"]) for node in item["nodes"]), 400)


async def read_review_queue(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    config: HubConfig,
    *,
    sink: str | None = None,
    limit: int = 100,
) -> Cleared:
    """The open items of ``project``'s review queue (those no one dismissed) in its newest graph, most important first,
    as ``sink`` may read them: each with the nodes it is about, their kind and name, and a redacted snippet of the
    text that made the rule find it. ``config`` is the hub's (``hub_config(request)`` or ``Context.config``)."""
    limit = _limit(limit)
    project_id, rules, chosen, why = await _gate(conn, plugin, project, sink)
    if why is not None:
        return Cleared(chosen, (), 0, why)
    data, build = await _graph(conn, config, project_id, project, rules, chosen, _queue)
    if data is None:
        return Cleared(chosen, (), 0, NO_GRAPH.format(project=project))
    if not data.get("computed"):
        return Cleared(chosen, (), 0, data.get("reason"))
    d = tables.kg_review_dismissals
    dismissed = set((await conn.execute(select(d.c.fingerprint).where(d.c.project_id == project_id))).scalars())
    props = data.get("props") or {}
    items = [
        ReviewItemText(
            item["fingerprint"],
            item["rule"],
            item["action"],
            tuple(ReviewNodeText(n["id"], n["kind"], n["name"]) for n in item["nodes"]),
            _snippet(item, props),
        )
        for item in data["items"]
        if item["fingerprint"] not in dismissed
    ]
    return Cleared(chosen, tuple(items[:limit]), 0, None if items else "no open item")


async def dismiss_review_item(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    config: HubConfig,
    fingerprint: str,
    reason: str,
    *,
    sink: str | None = None,
) -> DismissedItem:
    """Dismiss an item of the review queue the sink may read, with ``reason`` (1 to ``MAX_REASON_CHARS`` characters
    once trimmed), as the plugin: the dismissal names it and the audit row too. 404 for an item the sink cannot read, as
    for one that does not exist. A writer takes it back from the web (``DELETE .../review/<fingerprint>/dismiss``) or
    with ``evo-agents hub kg review undismiss``."""
    reason = _reason(reason)
    project_id, rules, chosen, why = await _gate(conn, plugin, project, sink)
    if why is not None:
        raise Refused(403, why)
    data, build = await _graph(conn, config, project_id, project, rules, chosen, _queue)
    if data is None:
        raise Refused(404, NO_GRAPH.format(project=project))
    item = next((i for i in data.get("items", []) if i["fingerprint"] == fingerprint), None)
    if item is None:
        raise Refused(
            404, f"no review item with that fingerprint is visible to plugin {plugin.id} in project {project}"
        )
    d = tables.kg_review_dismissals
    saved = pg_insert(d).values(
        project_id=project_id,
        fingerprint=fingerprint,
        rule=item["rule"],
        reason=reason,
        dismissed_by=None,
        dismissed_by_plugin=plugin.id,
        build_id=build,
    )
    await conn.execute(
        saved.on_conflict_do_update(
            index_elements=[d.c.project_id, d.c.fingerprint],
            set_={
                "rule": saved.excluded.rule,
                "reason": saved.excluded.reason,
                "dismissed_by": None,
                "dismissed_by_plugin": plugin.id,
                "build_id": saved.excluded.build_id,
                "dismissed_at": func.now(),
            },
        )
    )
    await audit_log.record(
        conn,
        actor_id=None,
        token_id=None,
        action=audit_log.KG_REVIEW,
        target=f"{project} item={fingerprint} plugin={plugin.id}",
        project_id=project_id,
    )
    return DismissedItem(fingerprint, item["rule"])


def _reason(reason: str) -> str:
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= MAX_REASON_CHARS:
        raise ValueError(f"a reason is 1 to {MAX_REASON_CHARS} characters")
    return reason.strip()


async def decide_graph(
    conn: AsyncConnection,
    plugin: Plugin,
    project: str,
    config: HubConfig,
    kind: str,
    src: str,
    *,
    rel: str | None = None,
    dst: str | None = None,
    reason: str,
    propose: bool = False,
    sink: str | None = None,
) -> DecisionTaken:
    """Take a decision on ``project``'s graph as the plugin: deprecate a node (``src``) or an edge (``src``, ``rel``,
    ``dst``), merge two node ids (``src``, ``dst``) or link two nodes (``src``, ``rel`` of the ontology, ``dst``),
    active at once and a build queued, or with ``propose`` only proposed, for a writer to accept or reject. The nodes
    and the edge must be ones the sink may read in the newest graph (404 otherwise); a decision that says the same is
    409. History and audit name the plugin, and a writer undoes the decision from the web and the CLI as any
    other."""
    reason = _reason(reason)
    if kind not in DECISION_KINDS:
        raise ValueError(f"a decision is one of {', '.join(DECISION_KINDS)}")
    for name, value in (("src", src), ("rel", rel), ("dst", dst)):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= kg_decisions.MAX_REF_CHARS):
            raise ValueError(f"{name} is 1 to {kg_decisions.MAX_REF_CHARS} characters")
    if kind == "merge" and (rel is not None or dst is None or dst == src):
        raise ValueError("a merge names two different nodes, src and dst, and no rel")
    if kind == "link" and (rel is None or dst is None):
        raise ValueError("a link names src, rel and dst")
    if kind == "deprecate" and (rel is None) != (dst is None):
        raise ValueError("a deprecation names a node (src) or an edge (src, rel and dst)")
    project_id, rules, chosen, why = await _gate(conn, plugin, project, sink)
    if why is not None:
        raise Refused(403, why)
    nodes, edge = kg_decisions.subject(kind, src, rel, dst)
    found, build = await _graph(
        conn,
        config,
        project_id,
        project,
        rules,
        chosen,
        lambda session: session.decision_subjects(nodes, [edge] if edge else []),
    )
    if found is None:
        raise Refused(404, NO_GRAPH.format(project=project))
    missing = [
        key
        for key in [*nodes, *([edge] if edge else [])]
        if found.get(key, kg_decisions.ABSENT) in (kg_decisions.ABSENT, kg_decisions.HIDDEN)
    ]
    if missing:
        what = " ".join(missing[0]) if isinstance(missing[0], tuple) else missing[0]
        raise Refused(404, f"{what} is not visible to plugin {plugin.id} in the latest graph of project {project}")
    if kind == "link":
        configs = tables.kg_configs
        ontology = await conn.scalar(select(configs.c.ontology).where(configs.c.project_id == project_id))
        if rel not in known_relations(ontology):
            raise Refused(422, f"the ontology of project {project} has no relation {rel}")
    d, e, p = tables.kg_decisions, tables.kg_decision_events, tables.projects
    await conn.execute(select(p.c.id).where(p.c.id == project_id).with_for_update())
    rev = await conn.scalar(select(func.coalesce(func.max(e.c.rev), 0)).where(e.c.project_id == project_id))
    same = select(d.c.number).where(
        d.c.project_id == project_id,
        d.c.kind == kind,
        d.c.src == src,
        d.c.rel.is_not_distinct_from(rel),
        d.c.dst.is_not_distinct_from(dst),
        d.c.state.in_(("proposed", "active")),
    )
    taken = await conn.scalar(same.limit(1))
    if taken is not None:
        raise Refused(409, f"decision {taken} of project {project} already says that")
    number = await conn.scalar(select(func.coalesce(func.max(d.c.number), 0) + 1).where(d.c.project_id == project_id))
    state, action = ("proposed", "propose") if propose else ("active", "decide")
    row = (
        await conn.execute(
            d.insert()
            .values(project_id=project_id, number=number, kind=kind, state=state, src=src, rel=rel, dst=dst)
            .returning(d.c.id)
        )
    ).one()
    await conn.execute(
        e.insert().values(
            project_id=project_id,
            rev=rev + 1,
            decision_id=row.id,
            action=action,
            actor_id=None,
            actor_plugin=plugin.id,
            reason=reason,
            build_id=build,
        )
    )
    await audit_log.record(
        conn,
        actor_id=None,
        token_id=None,
        action=audit_log.KG_DECISION,
        target=f"{project} decision={number} action={action} plugin={plugin.id}",
        project_id=project_id,
    )
    if not propose:  # the active decisions changed: the next build applies them
        await queue_if_configured(JobQueue.in_transaction(), conn, project_id, project)
    return DecisionTaken(number, kind, state, rev + 1)


__all__ = [
    "AcceptedRule",
    "CauseClassified",
    "Cleared",
    "CorrectionText",
    "DecisionTaken",
    "DismissedItem",
    "ReviewItemText",
    "ReviewNodeText",
    "RunEventText",
    "RunFailure",
    "classify_run_failure",
    "decide_graph",
    "dismiss_review_item",
    "read_accepted_rules",
    "read_corrections",
    "read_review_queue",
    "read_run_failure",
]
