"""The tools of the plugin canvas on the hub's /mcp, which @strategist's agent reads, edits, proposes, adds evidence
and judges with (criteria a5 and a4 of plan living-lean-canvas): ``canvas__read``, ``canvas__signals``,
``canvas__edit``, ``canvas__undo``, ``canvas__propose``, ``canvas__evidence`` and ``canvas__judge``.

The hub lists them in a session whose project enabled the plugin, and checks their arguments against the manifest's
schemas first (``tools``). Each acts for a person (``acting``); a refusal is the tool's error text, and nothing is
written.

- ``read`` and ``signals`` answer anyone with a grant on the project, through the session's sink.
- ``edit`` and ``undo`` answer the agent of a chat run of @strategist alone (the profile of the project's agent type
  ``strategist``, while it is on), for a person who holds writer: the member's command applies at once, written as an
  agent's rev for that member, with the run, its runtime and model and the message that asked for it. A claim it makes
  starts as an assumption. ``undo`` without claims takes back the member's last command (``commands``).
- ``propose`` and ``evidence`` answer the agent of a chat or task run of @strategist: a proposal changes nothing until
  a writer accepts it, so a task run (a brainstorm) changes no claim; a new claim proposed carries a test card with a
  hypothesis and a test. Evidence an agent adds counts toward validated only once a writer confirms it; a simulation
  is always synthetic. ``propose`` also answers the agent of the Curator's review run on a night of the lens product
  (``acting.proposing_run``; plan loop-engine, criterion a9), for the night shift's owner; every other tool that
  writes refuses it.
- ``judge`` answers the agent of a judging run alone, the task run the value check hands @strategist (``judging``):
  the items of its run, and a verdict with why on each. A judging run calls no other tool of the plugin: each refuses
  it, so it reads nothing but its items and changes, proposes and adds nothing.

Nothing here moves a claim to validated (``rules.check`` refuses an agent that, whoever asked), confirms evidence,
decides a proposal or a baseline: those routes refuse the token of a run. An edit or an undo queues a build of the
project's knowledge graph, as the routes do.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import judging, proposals, rules, store, writes
from evo_agents.core_plugins.canvas.acting import STRATEGIST, Acting, acting, canvas, proposing_run, strategist_run
from evo_agents.core_plugins.canvas.commands import undo_command, undone_text
from evo_agents.core_plugins.canvas.content import LINKS, CanvasError, code, number_of
from evo_agents.core_plugins.canvas.tools import AGENT_EVIDENCE_KINDS
from evo_agents.hub import signals, tables
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.signals import readable
from evo_agents.hub.tables_canvas import CELLS

SHOWN_REVS = 10  # of a claim, the latest
SHOWN_PROPOSALS = 50
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 50
FEEDBACK_CHARS = 300  # of a piece of feedback a series shows
RACE = "another write in the project took a claim's number at the same moment: nothing was written; try again"
CLAIM_FIELDS = ("cell", "text", "status", "confidence", "delivery", "position", "test_card", "archived", *LINKS)


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def _conflict_lines(detail) -> list[str]:
    lines = []
    for item in detail or []:
        if not isinstance(item, dict) or "changed_by" not in item:
            continue
        changes = "; ".join(f"{c['field']}: {c['before']!r} -> {c['after']!r}" for c in item.get("changes") or [])
        current = item.get("current") or {}
        lines.append(
            f"- {item['claim']} is at rev {item['rev']}, changed by {item['changed_by']}; you read rev "
            f"{item['base_rev']}{': ' + changes if changes else ''}. It now reads [{current.get('status')}] "
            f"{current.get('text')}"
        )
    return lines


async def _not_judging(conn: AsyncConnection, who: Acting, tool: str | None) -> None:
    """Refuse every tool but ``judge`` to the agent of a judging run: it sees its items alone, and changes nothing."""
    if who.run_id is not None and tool != "judge" and await judging.judging_run(conn, who.run_id):
        raise CanvasError(
            403,
            f"a judging run of @{STRATEGIST} calls canvas__judge alone: it reads, changes and proposes nothing else",
        )


async def _answer(params: dict, context: CoreContext, work) -> dict:
    """``work(conn, acting, arguments)`` in one transaction, its text the tool's answer; a refusal as its error."""
    try:
        async with context.engine.begin() as conn:
            who = await acting(conn, params, context)
            await _not_judging(conn, who, params.get("tool"))
            return _text(await work(conn, who, params.get("arguments") or {}))
    except CanvasError as exc:
        return _text("\n".join([f"error: {exc.message}", *_conflict_lines(exc.detail)]), error=True)
    except IntegrityError:
        return _text(f"error: {RACE}", error=True)


# Reading


def _card_text(card: dict | None) -> str:
    if not card:
        return "no test card"
    return "; ".join(f"{name} {value}" for name, value in card.items())


def _evidence_counts(rows: list[store.EvidenceRow]) -> str:
    live = [row for row in rows if row.withdrawn_at is None]
    real = rules.real_support([row.facts() for row in live])
    return f"{len(live)} piece(s) of evidence, {len(real)} real support" if live else "no evidence"


def _claim_lines(row: store.ClaimRow, evidence: list[store.EvidenceRow]) -> list[str]:
    archived = ", archived" if row.archived_at is not None else ""
    return [
        f"- {row.code} [{row.status}, {row.confidence}, rev {row.rev}{archived}] {row.text}",
        f"  delivery: {row.delivery}",
        f"  test card: {_card_text(row.test_card)}",
        *([f"  use cases: {', '.join(row.use_cases)}"] if row.use_cases else []),
        *([f"  signal keys: {', '.join(row.signal_keys)}"] if row.signal_keys else []),
        *([f"  problems: {', '.join(row.problems)}"] if row.problems else []),
        *([f"  opportunities: {', '.join(row.opportunities)}"] if row.opportunities else []),
        f"  {_evidence_counts(evidence)}",
    ]


def _proposal_line(row: proposals.ProposalRow) -> str:
    what = f"a change of {row.claim} at rev {row.base_rev}" if row.kind == "change" else "a new claim"
    synthetic = ", synthetic" if row.synthetic else ""
    who = row.provenance.login or row.provenance.actor
    run = f" in run #{row.provenance.run_id}" if row.provenance.run_id else ""
    return f"- proposal {row.id}: {what} ({', '.join(row.fields)}{synthetic}), by {who}{run}: {row.reason}"


async def _canvases(conn: AsyncConnection, who: Acting) -> str:
    rows = [row for row in await store.canvases(conn, who.access, who.sink) if who.sees(row.label)]
    if not rows:
        return f"Project {who.access.name} has no canvas that {who.person.login} sees."
    counts = await store.counts(conn, [row.id for row in rows])
    lines = [f"The canvases of project {who.access.name}:"]
    for row in rows:
        statuses = ", ".join(f"{n} {status}" for status, n in sorted(counts[row.id].items())) or "no claim"
        lines.append(f"- {row.slug}: {row.name}; {statuses}")
    return "\n".join(lines)


def _visible(who: Acting, rows: list[store.EvidenceRow]) -> list[store.EvidenceRow]:
    return [row for row in rows if who.sees(row.label)]


async def _canvas_text(conn: AsyncConnection, who: Acting, row: store.CanvasRow, archived: bool) -> str:
    claims = await store.claims(conn, row.id, with_archived=archived)
    evidence = await store.evidence(conn, [claim.id for claim in claims])
    lines = [f"{row.slug}: {row.name}" + (" (archived)" if row.archived_at else "")]
    if row.description:
        lines.append(row.description)
    for cell in CELLS:
        lines.append(f"{cell.replace('_', ' ').capitalize()}:")
        held = [claim for claim in claims if claim.cell == cell]
        for claim in held:
            lines += [f"  {line}" for line in _claim_lines(claim, _visible(who, evidence[claim.id]))]
        if not held:
            lines.append("  -")
    opened = await proposals.proposals(conn, row, ("open",))
    if opened:
        lines.append(f"Open proposals ({len(opened)}), which members accept or reject:")
        lines += [_proposal_line(item) for item in opened[:SHOWN_PROPOSALS]]
    return "\n".join(lines)


def _rev_line(rev: store.RevRow) -> str:
    who = rev.provenance.login or "the hub"
    if rev.provenance.actor == "agent":
        who = f"an agent for {who} (run #{rev.provenance.run_id})"
    reason = f": {rev.reason}" if rev.reason else ""
    return f"- rev {rev.rev} {rev.change} by {who}, {rev.created_at:%Y-%m-%d %H:%M}{reason}"


def _evidence_line(row: store.EvidenceRow) -> str:
    why = rules.not_real(row.facts())
    real = "real" if why is None else f"not real: {why}"
    synthetic = ", synthetic" if row.synthetic else ""
    return f"- evidence {row.id}: {row.kind} {row.direction} ({real}{synthetic}): {row.summary}"


async def _claim_text(conn: AsyncConnection, who: Acting, row: store.CanvasRow, ref: str) -> str:
    claim = await store.claim(conn, who.access, row, number_of(ref))
    evidence = _visible(who, (await store.evidence(conn, [claim.id]))[claim.id])
    revs = await store.revs(conn, claim.id)
    lines = [f"{claim.code} of canvas {row.slug}, cell {claim.cell}:", *_claim_lines(claim, evidence)]
    lines += ["Evidence:", *(_evidence_line(item) for item in evidence)] if evidence else []
    lines += [f"Revs (the last {min(len(revs), SHOWN_REVS)} of {len(revs)}):"]
    lines += [_rev_line(rev) for rev in revs[-SHOWN_REVS:]]
    opened = [item for item in await proposals.proposals(conn, row, ("open",)) if item.claim == claim.code]
    lines += ["Open proposals:", *(_proposal_line(item) for item in opened)] if opened else []
    return "\n".join(lines)


async def _read(conn: AsyncConnection, who: Acting, args: dict) -> str:
    if not args.get("canvas"):
        return await _canvases(conn, who)
    row = await canvas(conn, who, args["canvas"])
    if args.get("claim"):
        return await _claim_text(conn, who, row, args["claim"])
    return await _canvas_text(conn, who, row, bool(args.get("archived")))


async def read(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__read``: the canvases, a canvas by cell, or one claim whole."""
    return await _answer(params, context, _read)


def _value_text(row) -> str:
    if row.value is not None:
        return f"{row.value:g}"
    text = row.text or ""
    return repr(text if len(text) <= FEEDBACK_CHARS else text[:FEEDBACK_CHARS] + "...")


def _signal_line(row) -> str:
    extra = ", ".join(part for part in (row.tenant and f"tenant {row.tenant}", row.synthetic and "synthetic") if part)
    return f"- {row.measured_at:%Y-%m-%d %H:%M} {_value_text(row)}" + (f" ({extra})" if extra else "")


async def _rows(conn: AsyncConnection, who: Acting, **options) -> list:
    rows = await signals.signal_rows(conn, who.access.project_id, readable(who.access, who.sink), **options)
    return [row for row in rows if who.sees(row.label)]


async def _registry(conn: AsyncConnection, who: Acting) -> str:
    k = tables.signal_keys
    query = (
        select(k.c.name, k.c.kind, k.c.unit, k.c.description, k.c.source)
        .where(k.c.project_id == who.access.project_id, k.c.archived_at.is_(None))
        .order_by(k.c.name)
    )
    keys = list(await conn.execute(query))
    if not keys:
        return f"Project {who.access.name} declares no signal key: an admin adds them with `evo-agents hub signal keys add`."
    lines = [f"The signal keys of project {who.access.name}:"]
    for key in keys:
        unit = f", {key.unit}" if key.unit else ""
        latest = await _rows(conn, who, key=key.name, limit=1)
        last = f"latest {_value_text(latest[0])} at {latest[0].measured_at:%Y-%m-%d %H:%M}" if latest else "no value"
        about = f": {key.description}" if key.description else ""
        source = f"; from {key.source}" if key.source else ""
        lines.append(f"- {key.name} ({key.kind}{unit}){about}{source}; {last}")
    return "\n".join(lines)


async def _signals(conn: AsyncConnection, who: Acting, args: dict) -> str:
    store.reader(who.access)
    if not args.get("key"):
        return await _registry(conn, who)
    days, limit = args.get("days") or DEFAULT_DAYS, args.get("limit") or DEFAULT_LIMIT
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await _rows(conn, who, key=args["key"], since=since, tenant=args.get("tenant"), limit=limit)
    if not rows:
        return f"No value of {args['key']} in the last {days} days that {who.person.login} may read."
    return "\n".join([f"{args['key']}, the last {days} days, newest first ({len(rows)}):", *map(_signal_line, rows)])


async def find_signals(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__signals``: the registry of keys with their latest values, or one key's series."""
    return await _answer(params, context, _signals)


# Writing


def _change(item: dict) -> writes.Change:
    values = {name: item[name] for name in CLAIM_FIELDS if name in item}
    return writes.Change(item.get("claim"), item.get("base_rev"), values)


def _no_status_on_new(changes: list[writes.Change]) -> None:
    for change in changes:
        if change.claim is None and ("status" in change.values or change.values.get("archived")):
            raise CanvasError(422, "a claim @strategist makes starts as an assumption, not archived: leave status out")


def _outcomes_text(who: Acting, outcomes: list[writes.Outcome], rows: dict[int, store.ClaimRow]) -> str:
    lines = [f"Written at once as {who.person.login}'s, by run #{who.run_id}:"]
    for outcome in outcomes:
        row = rows[outcome.number]
        state = f"rev {row.rev}" if outcome.written else f"unchanged at rev {row.rev}"
        lines.append(f"- {row.code} [{row.status}] {state}: {row.text}")
    lines.append("The member takes it back by asking you to undo it (canvas__undo without claims).")
    return "\n".join(lines)


async def _edit(conn: AsyncConnection, who: Acting, args: dict) -> str:
    strategist_run(who, "edit", ("chat",))
    store.writer(who.access, "editing claims")
    row = await canvas(conn, who, args["canvas"], lock=True)
    changes = [_change(item) for item in args["changes"]]
    _no_status_on_new(changes)
    outcomes = await writes.edit(conn, who.writer(), row, changes, args["reason"])
    found = {o.number: await store.claim(conn, who.access, row, o.number) for o in outcomes}
    return _outcomes_text(who, outcomes, found)


def _changing(context: CoreContext, work):
    """``work``, then a build of the project's knowledge graph queued, whose claims the plugin's source gives."""

    async def changing(conn: AsyncConnection, who: Acting, args: dict) -> str:
        text = await work(conn, who, args)
        if context.jobs is not None:
            await queue_if_configured(context.jobs, conn, who.access.project_id, who.access.name)
        return text

    return changing


async def edit(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__edit``: the member's command, applied at once as an agent's revs for them."""
    return await _answer(params, context, _changing(context, _edit))


async def _undo(conn: AsyncConnection, who: Acting, args: dict) -> str:
    strategist_run(who, "undo", ("chat",))
    store.writer(who.access, "undoing a change")
    row = await canvas(conn, who, args["canvas"], lock=True)
    reason = args.get("reason") or ""
    if args.get("claims"):
        targets = [(item["claim"], item["rev"]) for item in args["claims"]]
        outcomes = await writes.undo(conn, who.writer(), row, targets, reason)
        found = {o.number: await store.claim(conn, who.access, row, o.number) for o in outcomes}
        return _outcomes_text(who, outcomes, found)
    done = await undo_command(conn, who.writer(), row, reason)
    lines = [f"Took back the command of chat message {done.message_id}, as {who.person.login}'s, by run #{who.run_id}:"]
    return "\n".join([*lines, *undone_text(done)])


async def undo(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__undo``: the member's last command taken back, or the current rev of claims named."""
    return await _answer(params, context, _changing(context, _undo))


def _cards(changes: list[writes.Change]) -> None:
    for change in changes:
        card = change.values.get("test_card") or {}
        if change.claim is None and not (card.get("hypothesis") and card.get("test")):
            raise CanvasError(
                422, "a proposed claim carries a test card with at least a hypothesis and a test: what would show it"
            )


async def _propose(conn: AsyncConnection, who: Acting, args: dict) -> str:
    await proposing_run(conn, who)
    store.writer(who.access, "proposing claims")
    row = await canvas(conn, who, args["canvas"], lock=True)
    changes = [_change(item) for item in args["proposals"]]
    _no_status_on_new(changes)
    _cards(changes)
    made = await proposals.propose(conn, who.writer(), row, changes, args["reason"], synthetic=args["synthetic"])
    held = {item.id: item for item in await proposals.proposals(conn, row, ("open",))}
    lines = [f"{len(made)} proposal(s) of canvas {row.slug}, open for members to accept or reject:"]
    lines += [_proposal_line(held[proposal_id]) for proposal_id in made]
    lines.append(f"Nothing changed yet: `evo-agents hub canvas proposal list {row.slug} --project {who.access.name}`.")
    return "\n".join(lines)


async def propose(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__propose``: new claims or changes of claims, for members to accept or reject."""
    return await _answer(params, context, _propose)


async def _evidence(conn: AsyncConnection, who: Acting, args: dict) -> str:
    strategist_run(who, "evidence", ("chat", "task"))
    store.writer(who.access, "adding evidence")
    if args["kind"] not in AGENT_EVIDENCE_KINDS:
        raise CanvasError(422, f"@{STRATEGIST} adds evidence of kind {', '.join(AGENT_EVIDENCE_KINDS)} only")
    row = await canvas(conn, who, args["canvas"], lock=True)
    store.writable(who.access, row)
    claim = await store.claim(conn, who.access, row, number_of(args["claim"]))
    on = date.fromisoformat(args["observed_on"]) if args.get("observed_on") else None
    given = writes.EvidenceInput(
        args["kind"], args["direction"], args["summary"], args.get("url"), on, args["synthetic"]
    )
    evidence_id = await writes.add_evidence(conn, who.writer(), claim, given)
    synthetic = args["synthetic"] or args["kind"] == "simulation"
    return (
        f"Evidence {evidence_id} of {code(claim.number)}: {args['kind']}, {args['direction']}"
        f"{', synthetic' if synthetic else ''}, added by run #{who.run_id} for {who.person.login}. It counts toward "
        "validated only once a writer confirms it" + (", and never, being synthetic." if synthetic else ".")
    )


async def evidence(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__evidence``: a piece of evidence of a claim, which a writer confirms."""
    return await _answer(params, context, _evidence)


# Judging


async def judge(params: dict, context: CoreContext) -> dict:
    """The tool ``canvas__judge``: the items of the caller's judging run, and a verdict with why on some of them."""

    async def work(conn: AsyncConnection, who: Acting, args: dict) -> str:
        return await judging.judge(conn, who, args, context.config)

    return await _answer(params, context, work)


__all__ = ["edit", "evidence", "find_signals", "judge", "propose", "read", "undo"]
