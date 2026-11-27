"""The playbook of the Curator on the hub (``evo_agents.hub.playbook`` holds the model): its entries in
curator_playbook, and every delta in curator_playbook_deltas, never changed or deleted.

POST /v1/worker/runs/{id}/playbook takes one delta of the agent of a review run the worker holds (404 for any other
run), as the member who dispatched it, who must still hold writer on the project, as ``proposals.record_finding``
takes a finding: ``add`` a new entry (a lens of ``review.LENSES`` or none, its text, and 1 to MAX_EVIDENCE pieces of
evidence, which the hub resolves as a finding's: 422 for one it cannot find), stored with the project's default label
and the proposal it holds until (``until_proposal_id``, a proposal of the project: 422 for any other; without it, the
one its text names, ``playbook.until_proposal``, when the project has it);
``helpful`` or ``harmful`` one more on the counter of a live entry, with evidence when given, once each way for one run
(409 for the second); ``retire`` a live entry, with a note why. The project holds at most MAX_LIVE live entries, and a
run writes at most MAX_DELTAS deltas (409 past either). Each delta writes a line in the run's log.

GET /v1/projects/{p}/curator/playbook (reader) lists the entries the caller may read through the sink X-Evo-Sink names
(else the project's hub sink), the most helpful first, retired ones with ``retired``; the MCP tool curator_playbook and
``evo-agents hub curator playbook`` read it. ``for_prompt`` gives the lines of the playbook in a review run's prompt,
as its owner reads it, and writes in the run's log which entries they are (``playbook_shown``); once that run is done,
``exposed`` counts one more on ``prompt_exposures`` of each.

``retire_due`` is the playbook's maintenance, after each review run that is done (``exposed``) and each change of the
Curator that is merged (``ledger.change_moved``): it retires each live entry ``playbook.due_retires`` names, the one
that held until a proposal whose change is merged and the one shown in EXPOSURE_LIMIT review runs and never found
helpful, with a delta ``retire`` of actor CURATOR_ACTOR and no run, as an agent's retire otherwise.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import playbook, review
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.playbook import repository as store
from evo_agents.hub.server.playbook.models import PlaybookEntry, PlaybookView
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposals.models import EvidenceIn
from evo_agents.hub.server.proposals.service.record import held_review_run, resolve, writer_of
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


class PlaybookDelta(BaseModel):
    """One delta of the playbook (``evo_agents.hub.playbook.OPS``)."""

    op: Literal[playbook.OPS]  # pyright: ignore[reportInvalidTypeForm]
    entry_id: int | None = Field(None, ge=1, le=MAX_ID, description="helpful, harmful, retire: the entry")
    lens: Literal[tuple(review.LENSES)] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="add: the lens it is for; none: every lens"
    )
    text: str | None = Field(None, min_length=1, max_length=playbook.MAX_TEXT_CHARS, description="add: the lesson")
    evidence: list[EvidenceIn] = Field(default_factory=list, max_length=review.MAX_EVIDENCE)
    note: str | None = Field(None, min_length=1, max_length=playbook.MAX_NOTE_CHARS, description="retire: why")
    until_proposal_id: int | None = Field(
        None, ge=1, le=MAX_ID, description="add: the proposal it holds until; the merge of its change retires it"
    )

    @model_validator(mode="after")
    def _complete(self):
        if self.op == "add":
            if self.entry_id is not None or not self.text or not self.evidence:
                raise ValueError("add takes the text and at least one piece of evidence, and no entry_id")
            self.text = playbook.clean_text(self.text)
        else:
            if self.entry_id is None or self.text is not None or self.lens is not None or self.until_proposal_id:
                raise ValueError(f"{self.op} takes the entry_id of an entry, and neither text nor lens nor until")
            if self.op == "retire" and not self.note:
                raise ValueError("retire takes a note: why the entry no longer holds")
        return self


def _entry(row) -> PlaybookEntry:
    data = {key: value for key, value in row._mapping.items() if key not in ("label", "retired_at")}
    return PlaybookEntry(**data, ref=playbook.ref(row.id), retired=row.retired_at is not None)


async def _until(conn: AsyncConnection, project_id: int, body: PlaybookDelta) -> int | None:
    """The proposal the new entry holds until: the one ``body`` names, which must be of the project (422 otherwise),
    else the one its text names when the project has it, else None."""
    until = body.until_proposal_id or playbook.until_proposal(body.text)
    if until is None or await store.is_proposal_of(conn, until, project_id):
        return until
    if body.until_proposal_id is not None:
        raise Unprocessable(f"until_proposal_id {until} is not a proposal of this project")
    return None


async def _add(conn, request, run, access, body: PlaybookDelta) -> tuple[int, list[dict]]:
    if await store.live_count(conn, run.project_id) >= playbook.MAX_LIVE:
        raise Conflict(f"project {run.project} has {playbook.MAX_LIVE} live entries in its playbook: retire one first")
    evidence = await resolve(conn, request, run, access, body.evidence)
    values = {
        "project_id": run.project_id,
        "lens": body.lens,
        "text": body.text,
        "evidence": evidence,
        "label": access.push_label(),
        "created_run_id": run.id,
        "updated_run_id": run.id,
        "until_proposal_id": await _until(conn, run.project_id, body),
    }
    return await store.insert_entry(conn, values), evidence


async def _change(conn, request, run, access, body: PlaybookDelta) -> tuple[int, list[dict]]:
    entry_id = body.entry_id
    assert entry_id is not None  # every op but add names its entry (PlaybookDelta)
    row = await store.lock_entry(conn, entry_id, run.project_id)
    if row is None:
        raise NotFound(f"the playbook of project {run.project} has no entry {playbook.ref(entry_id)}")
    if row.retired_at is not None:
        raise Conflict(f"entry {playbook.ref(entry_id)} is retired: add a new entry instead")
    if body.op in playbook.TAGS:
        if await store.counted_already(conn, entry_id, run.id, body.op):
            raise Conflict(f"run {run.id} counted entry {playbook.ref(entry_id)} {body.op} already: once a run")
    evidence = await resolve(conn, request, run, access, body.evidence) if body.evidence else []
    await store.change_entry(conn, entry_id, run.id, body.op, body.note)
    return entry_id, evidence


async def record_delta(conn: AsyncConnection, request, user: Principal, run_id: int, body: PlaybookDelta):
    """One delta of the playbook from the agent of the review run this worker holds: the entry, and its id."""
    run = await held_review_run(conn, user, run_id)
    access = await writer_of(conn, run)
    if await store.run_deltas(conn, run_id) >= playbook.MAX_DELTAS:
        raise Conflict(f"run {run_id} wrote {playbook.MAX_DELTAS} deltas of the playbook, the most a run may")
    if body.op == "add":
        entry_id, evidence = await _add(conn, request, run, access, body)
    else:
        entry_id, evidence = await _change(conn, request, run, access, body)
    delta = {"project_id": run.project_id, "entry_id": entry_id, "run_id": run_id, "op": body.op}
    await store.insert_delta(conn, {**delta, "evidence": evidence, "note": body.note})
    row = await store.entry(conn, entry_id)
    what = {"add": "added", "helpful": "counted helpful", "harmful": "counted harmful", "retire": "retired"}
    text = f"playbook entry {playbook.ref(entry_id)} {what[body.op]}: {row.text}"
    await write_event(conn, run_id, {"text": text, "playbook": entry_id, "op": body.op})
    return _entry(row), entry_id


# Reading


async def entries_of(conn: AsyncConnection, access: ProjectAccess, through: str | None) -> list:
    rows = await store.entries_of(conn, access.project_id)
    return [row for row in rows if through is not None and access.visible(row.label, through)]


def _ordered(rows, retired: bool, lens: str | None) -> list:
    if lens is not None:
        rows = [row for row in rows if row.lens in (None, lens)]
    live = playbook.ranked([row._mapping for row in rows])
    gone = [row._mapping for row in rows if row.retired_at is not None] if retired else []
    return [*live, *gone]


async def playbook_view(
    conn: AsyncConnection, user: Principal, project: str, sink: str | None, *, lens: str | None, retired: bool
) -> PlaybookView:
    access = await project_access(conn, user, project)
    need_reader(access)
    rows = await entries_of(conn, access, plan_service.read_sink(access, sink))
    by_id = {row.id: row for row in rows}
    shown = [_entry(by_id[item["id"]]) for item in _ordered(rows, retired, lens)]
    live = sum(1 for row in rows if row.retired_at is None)
    return PlaybookView(project=access.name, entries=shown, live=live, retired=len(rows) - live)


async def for_prompt(
    conn: AsyncConnection, access: ProjectAccess, lenses: list[str], run_id: int | None = None
) -> list[str]:
    """The lines of the playbook in the prompt of a review run of ``lenses``, as its owner (``access``) reads it; with
    ``run_id``, the ids of the entries they show go into the log of that run (``playbook_shown``), which ``exposed``
    reads once it is done."""
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    rows = await entries_of(conn, access, through)
    shown = playbook.ranked([row._mapping for row in rows], lenses)[: playbook.MAX_SHOWN]
    if run_id is not None and shown:
        ids = [int(entry["id"]) for entry in shown]
        refs = ", ".join(playbook.ref(entry_id) for entry_id in ids)
        await write_event(conn, run_id, {"text": f"the prompt shows playbook entries {refs}", "playbook_shown": ids})
    return [playbook.line_of(entry) for entry in shown]


# The maintenance


async def exposed(conn: AsyncConnection, run_id: int, project_id: int) -> list[int]:
    """Review run ``run_id`` of project ``project_id`` is done: one more on ``prompt_exposures`` of each entry its
    prompt showed, as its last ``playbook_shown`` names them, then ``retire_due``; the entries it retired."""
    shown = await store.last_shown(conn, run_id)
    ids = [item for item in shown or [] if isinstance(item, int) and not isinstance(item, bool)]
    if ids:
        await store.count_exposures(conn, project_id, ids)
    return await retire_due(conn, project_id)


async def exposure_limit_of(conn: AsyncConnection, project_id: int) -> int:
    """The review runs that show an entry never found helpful before it is retired: the charter's
    ``evolve.exposure_limit``, ``playbook.EXPOSURE_LIMIT`` when it says none; 0 retires none that way."""
    body = await store.newest_charter_body(conn, project_id) or {}
    held = (body.get("evolve") or {}).get("exposure_limit") if isinstance(body, dict) else None
    return held if isinstance(held, int) and not isinstance(held, bool) and held >= 0 else playbook.EXPOSURE_LIMIT


async def retire_due(conn: AsyncConnection, project_id: int, exposure_limit: int | None = None) -> list[int]:
    """Retire each live entry of project ``project_id`` that ``playbook.due_retires`` names, under the rows' locks,
    with a delta ``retire`` of actor CURATOR_ACTOR and no run: the one that holds until a proposal whose change is
    merged (its ``until_proposal_id``, else what its text says, for an entry older than the column), and the one shown
    in ``exposure_limit`` review runs that none found helpful (``exposure_limit_of`` when None). The ids it retired,
    oldest first."""
    if exposure_limit is None:
        exposure_limit = await exposure_limit_of(conn, project_id)
    entries = await store.lock_live(conn, project_id)
    targets = {until for until in map(playbook.until_of, entries) if until is not None}
    landed: dict[int, str] = {}
    if targets:
        landed = await store.change_states(conn, project_id, targets)
    due = playbook.due_retires(entries, landed, exposure_limit)
    for entry_id, note in due:
        await store.retire(conn, entry_id, note)
        delta = {"project_id": project_id, "entry_id": entry_id, "op": "retire", "note": note}
        await store.insert_delta(conn, {**delta, "run_id": None, "evidence": [], "actor": playbook.CURATOR_ACTOR})
        log.info("playbook entry retired by the curator", extra={"project_id": project_id, "entry_id": entry_id})
    return [entry_id for entry_id, _ in due]
