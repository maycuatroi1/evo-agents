"""Recording the outputs of runs (``evo_agents.hub.outputs``) and tying them to questions and chat messages; the routes
are ``run_outputs``'s, the bytes ``output_content``'s.

A machine sends outputs in two calls, as the run's log goes (``run_events``): ``issue`` hands out the presigned PUTs
of the blobs of kind run-output the project does not hold yet (``blobs.issue_uploads``), ``record`` commits those
uploads (``blobs.commit_uploads``) and records the outputs, those whose bytes the project held already (an item with
no ``upload_id``) included. Both hold the run's limits against what it holds (MAX_RUN_OUTPUTS outputs and
MAX_RUN_BYTES, a SHA-256 the run holds counting once; 413 naming the limit) and refuse an output of source tool when
the project's switch is off (409). ``record`` writes the rows holding the run's row lock and the blob lock shared, so a
deletion of the blob cannot slip in between finding it held and the row that refers to it (``blob_gc``): an item whose
bytes the project no longer holds then is 409, to upload again. A file the run holds already (the same SHA-256) is the
output it is, with the source of the two that is kept longer (``outputs.KEPT_ORDER``) and the caption and tool call
it had, or these when it had none. Each call that adds or changes outputs writes one ``system`` event in the run's log
naming them (``outputs``: their ids), which wakes the run's streams.

A question carries the outputs of its run that its ``output_ids`` name (``link_decision``), and a chat message of a
run's agent those its ``output_ids`` name (``link_message``), never one of source tool there (422). A chat run's
outputs that it sent or left in its outputs directory go under its messages besides: under the next message it posts,
and once it ended, under its last one (``attach_leftovers``), whose chat gets a ``message_changed`` event.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, Field, field_validator
from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, chats, outputs, runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server import output_notices, output_views
from evo_agents.hub.server.blobs import Mismatch, Sha256, UploadItem, Uploads, commit_uploads, issue_uploads
from evo_agents.hub.server.chat_store import write_event as write_chat_event
from evo_agents.hub.server.errors import error_response
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

TOOL_IMAGES_OFF = (
    "project {project} takes no images out of tools' results: an admin of it turned them off "
    "(PUT /v1/projects/{project}/output-settings)"
)
NOT_HELD = (
    "project {project} does not hold the bytes of {sha256} (any more): ask for an upload of them "
    "(POST .../output-uploads) and record the output with its upload_id"
)
NAMES_IN_EVENT = 5  # names of the outputs a system event of the run's log shows


# Models


class OutputUploadItem(BaseModel):
    sha256: Sha256
    size: int = Field(ge=0, description=f"bytes, at most {outputs.MAX_FILE_BYTES}")
    source: Literal[outputs.SOURCES]


class OutputUploadRequest(BaseModel):
    items: list[OutputUploadItem] = Field(min_length=1, max_length=outputs.MAX_BATCH)


class OutputIn(BaseModel):
    sha256: Sha256
    size: int = Field(ge=0, description=f"bytes, at most {outputs.MAX_FILE_BYTES}")
    source: Literal[outputs.SOURCES]
    name: str = Field(min_length=1, max_length=outputs.MAX_NAME_CHARS, description="the file's name, or its path")
    media_type: str = Field(min_length=3, max_length=255, description="type/subtype, no parameters")
    caption: str | None = Field(None, max_length=outputs.MAX_CAPTION_CHARS)
    tool_call_id: str | None = Field(
        None, max_length=outputs.MAX_TOOL_CALL_CHARS, description="the tool call of the run's trace it came from"
    )
    upload_id: UUID4 | None = Field(
        None, description="the upload of its bytes (POST .../output-uploads); null when the project holds them already"
    )

    _name = field_validator("name")(outputs.check_name)
    _media_type = field_validator("media_type")(outputs.check_media_type)
    _caption = field_validator("caption")(outputs.check_caption)

    @field_validator("tool_call_id")
    @classmethod
    def _tool_call(cls, value: str | None) -> str | None:
        return outputs.check_line(value, outputs.MAX_TOOL_CALL_CHARS, "a tool call id")


class OutputBatch(BaseModel):
    outputs: list[OutputIn] = Field(min_length=1, max_length=outputs.MAX_BATCH)


class RecordedOutputs(BaseModel):
    outputs: list[RunOutput] = Field(description="in the order of the request; a file the run held is the one it is")
    added: int = Field(description="outputs new to the run")


# The run outputs go to


@dataclass(frozen=True)
class Target:
    """The run outputs are recorded on, its row locked, as the worker that claimed it or the member whose local run it
    is reaches it."""

    run_id: int
    project: str
    project_id: int
    kind: str
    state: str


Reach = Callable[[AsyncConnection], Awaitable[Target]]


async def target_of(conn: AsyncConnection, run_id: int, project: str) -> Target:
    """The ``Target`` of run ``run_id`` of ``project``, whose row the caller holds locked."""
    r = tables.runs
    row = (await conn.execute(select(r.c.project_id, r.c.kind, r.c.state).where(r.c.id == run_id))).one()
    return Target(run_id, project, row.project_id, row.kind, row.state)


async def tool_images(conn: AsyncConnection, project_id: int) -> bool:
    """Whether project ``project_id`` takes the images its machines take out of tools' results."""
    p = tables.projects
    return bool((await conn.execute(select(p.c.output_tool_images).where(p.c.id == project_id))).scalar_one())


async def usage(conn: AsyncConnection, run_id: int) -> tuple[int, int]:
    """(outputs, bytes) run ``run_id`` holds."""
    o = tables.run_outputs
    query = select(func.count(), func.coalesce(func.sum(o.c.size), 0)).where(o.c.run_id == run_id)
    count, size = (await conn.execute(query)).one()
    return int(count), int(size)


async def _check_sources(conn: AsyncConnection, target: Target, sources: set[str]) -> None:
    if outputs.TOOL in sources and not await tool_images(conn, target.project_id):
        raise HTTPException(409, TOOL_IMAGES_OFF.format(project=target.project))


async def _over_limits(
    request: Request, conn: AsyncConnection, target: Target, items: dict[str, int]
) -> JSONResponse | None:
    """The 413 answer when the files ``items`` ({sha256: size}) would take run ``target`` past a limit; None when
    they fit. A SHA-256 the run holds counts once."""
    o = tables.run_outputs
    held = select(o.c.sha256).where(o.c.run_id == target.run_id, one_of(o.c.sha256, list(items)))
    new = {sha: size for sha, size in items.items() if sha not in set((await conn.execute(held)).scalars())}
    big = [sha for sha, size in new.items() if size > outputs.MAX_FILE_BYTES]
    count, size = await usage(conn, target.run_id)
    if big:
        limit, message = "file_bytes", f"a file is at most {outputs.MAX_FILE_BYTES} bytes, and {big[0]} is larger"
    elif count + len(new) > outputs.MAX_RUN_OUTPUTS:
        limit = "outputs_per_run"
        message = f"run {target.run_id} holds {count} outputs, and a run holds at most {outputs.MAX_RUN_OUTPUTS}"
    elif size + sum(new.values()) > outputs.MAX_RUN_BYTES:
        limit = "bytes_per_run"
        message = f"run {target.run_id} holds {size} bytes of outputs, and a run holds at most {outputs.MAX_RUN_BYTES}"
    else:
        return None
    detail = [{"limit": limit, "outputs": count, "bytes": size, "asked": len(new), "asked_bytes": sum(new.values())}]
    return error_response(request, 413, f"{message}: nothing of this request was taken", detail=detail)


# Uploading and recording


async def issue(
    request: Request, user: Principal, reach: Reach, items: list[OutputUploadItem]
) -> Uploads | JSONResponse:
    """The presigned PUTs of the blobs of ``items`` the run's project does not hold yet, once the run reached by
    ``reach`` takes them: within its limits, and none of source tool while the project's switch is off."""
    async with request.app.state.engine.begin() as conn:
        target = await reach(conn)
        await _check_sources(conn, target, {item.source for item in items})
        refusal = await _over_limits(request, conn, target, {item.sha256: item.size for item in items})
    if refusal is not None:
        return refusal
    blobs = [UploadItem(sha256=item.sha256, size=item.size, kind=outputs.KIND) for item in items]
    return await issue_uploads(request, user, target.project, blobs)


def _distinct(items: list[OutputIn]) -> list[OutputIn]:
    """``items`` without a second item of a SHA-256, the first kept; 422 for one declared with two sizes."""
    seen: dict[str, OutputIn] = {}
    for item in items:
        if seen.setdefault(item.sha256, item).size != item.size:
            raise HTTPException(422, f"output {item.sha256} is declared twice with different sizes")
    return list(seen.values())


async def _declared(conn: AsyncConnection, items: list[OutputIn]) -> None:
    """422 unless each item with an upload names the upload of its own bytes: its SHA-256, size and kind."""
    named = {str(item.upload_id): item for item in items if item.upload_id is not None}
    if not named:
        return
    u = tables.blob_uploads
    query = select(u.c.upload_id, u.c.sha256, u.c.size, u.c.kind).where(u.c.upload_id.in_([UUID(i) for i in named]))
    found = {str(row.upload_id): row for row in (await conn.execute(query)).all()}
    for upload_id, item in named.items():
        row = found.get(upload_id)
        if row is not None and (row.sha256, row.size, row.kind) != (item.sha256, item.size, outputs.KIND):
            raise HTTPException(422, f"upload {upload_id} is not the upload of output {item.sha256} and its size")


async def _commit(request: Request, user: Principal, project: str, items: list[OutputIn]) -> JSONResponse | None:
    """Commit the uploads of ``items``; the 422 answer when one does not match what was declared."""
    upload_ids = sorted({str(item.upload_id) for item in items if item.upload_id is not None})
    if not upload_ids:
        return None
    try:
        await commit_uploads(request, user, project, upload_ids, kinds=frozenset({outputs.KIND}))
    except Mismatch as exc:
        message = (
            f"{len(exc.problems)} upload(s) do not match what was declared, such as {exc.problems[0]['upload_id']}: "
            f"{exc.problems[0]['problem']}. No output was recorded and the uploads were discarded; ask for new ones"
        )
        return error_response(request, 422, message, detail=exc.problems)
    return None


async def _held(conn: AsyncConnection, target: Target, items: list[OutputIn]) -> None:
    """409 unless the run's project holds the bytes of every item, with its size. Holding the blob lock shared."""
    b = tables.blobs
    query = select(b.c.sha256, b.c.size).where(
        b.c.project_id == target.project_id, blob_gc.hashes_in(b.c.sha256, [item.sha256 for item in items])
    )
    held = {row.sha256: row.size for row in (await conn.execute(query)).all()}
    for item in items:
        if held.get(item.sha256) != item.size:
            raise HTTPException(409, NOT_HELD.format(project=target.project, sha256=item.sha256))


def _merged(row, item: OutputIn) -> dict:
    """What of output ``row`` changes when ``item`` is the same file sent again."""
    changed = {}
    if outputs.KEPT_ORDER[item.source] > outputs.KEPT_ORDER[row.source]:
        changed["source"] = item.source
    if row.caption is None and item.caption is not None:
        changed["caption"] = item.caption
    if row.tool_call_id is None and item.tool_call_id is not None:
        changed["tool_call_id"] = item.tool_call_id
    return changed


async def _write(conn: AsyncConnection, target: Target, items: list[OutputIn]) -> tuple[list[int], list[int]]:
    """(the ids of the outputs of ``items`` in their order, the ids added or changed) after writing them."""
    o = tables.run_outputs
    query = select(o.c.id, o.c.sha256, o.c.source, o.c.caption, o.c.tool_call_id).where(
        o.c.run_id == target.run_id, one_of(o.c.sha256, [item.sha256 for item in items])
    )
    held = {row.sha256: row for row in (await conn.execute(query.with_for_update())).all()}
    ids, touched = [], []
    for item in items:
        row = held.get(item.sha256)
        if row is not None:
            changed = _merged(row, item)
            if changed:
                await conn.execute(update(o).values(**changed).where(o.c.id == row.id))
                touched.append(row.id)
            ids.append(row.id)
            continue
        fields = item.model_dump(exclude={"upload_id"})
        added = insert(o).values(run_id=target.run_id, project_id=target.project_id, **fields).returning(o.c.id)
        output_id = (await conn.execute(added)).scalar_one()
        ids.append(output_id)
        touched.append(output_id)
    return ids, touched


async def _note(conn: AsyncConnection, target: Target, items: list[OutputIn], ids: list[int], touched: list[int]):
    names = [item.name for item, output_id in zip(items, ids, strict=True) if output_id in touched]
    shown = ", ".join(names[:NAMES_IN_EVENT]) + (
        f" and {len(names) - NAMES_IN_EVENT} more" if len(names) > NAMES_IN_EVENT else ""
    )
    text = f"{len(touched)} output(s) recorded: {shown}"
    await write_event(conn, target.run_id, {"text": text, "outputs": touched})


async def record(request: Request, user: Principal, reach: Reach, batch: OutputBatch) -> RecordedOutputs | JSONResponse:
    """Commit the uploads of ``batch`` and record its outputs on the run ``reach`` reaches (see the module)."""
    items = _distinct(batch.outputs)
    sizes = {item.sha256: item.size for item in items}
    engine = request.app.state.engine
    async with engine.begin() as conn:  # the refusals before any upload is read
        target = await reach(conn)
        await _check_sources(conn, target, {item.source for item in items})
        refusal = await _over_limits(request, conn, target, sizes)
        await _declared(conn, items)
    refusal = refusal or await _commit(request, user, target.project, items)
    if refusal is not None:
        return refusal
    async with engine.begin() as conn:
        target = await reach(conn)  # the run's row locked again: the grant or the worker may have gone meanwhile
        await blob_gc.lock_shared(conn)
        await _check_sources(conn, target, {item.source for item in items})
        await _held(conn, target, items)
        refusal = await _over_limits(request, conn, target, sizes)
        if refusal is not None:
            return refusal
        before = await usage(conn, target.run_id)
        ids, touched = await _write(conn, target, items)
        if touched:
            await _note(conn, target, items, ids, touched)
            await output_notices.outputs_recorded(conn, target.run_id, touched)
        if target.kind == "chat" and target.state in runs.TERMINAL_STATES:
            await attach_leftovers(conn, target.run_id)
        added = (await usage(conn, target.run_id))[0] - before[0]
        rows = await output_views.of_run(conn, target.project, target.run_id)
    by_id = {output.id: output for output in rows}
    log.info("run outputs recorded", extra={"run_id": target.run_id, "outputs": len(ids), "added": added})
    return RecordedOutputs(outputs=[by_id[output_id] for output_id in ids], added=added)


# Questions and chat messages


async def _of_run(conn: AsyncConnection, run_id: int, output_ids: list[int], *, chat: bool = False) -> list[int]:
    """``output_ids`` without repeats, when each is an output of run ``run_id`` (and, for a chat message, of no
    source tool); 422 otherwise."""
    wanted = list(dict.fromkeys(output_ids))
    if not wanted:
        return []
    o = tables.run_outputs
    rows = (await conn.execute(select(o.c.id, o.c.source).where(o.c.run_id == run_id, one_of(o.c.id, wanted)))).all()
    sources = {row.id: row.source for row in rows}
    missing = [str(output_id) for output_id in wanted if output_id not in sources]
    if missing:
        raise HTTPException(422, f"run {run_id} has no output {', '.join(missing)}: name outputs of the run itself")
    tools = [str(output_id) for output_id in wanted if sources[output_id] == outputs.TOOL]
    if chat and tools:
        raise HTTPException(
            422, f"outputs {', '.join(tools)} are images taken out of tools' results: none goes to a chat"
        )
    return wanted


async def _link(conn: AsyncConnection, column: str, target_id: int, output_ids: list[int]) -> None:
    if not output_ids:
        return
    k = tables.run_output_links
    rows = [{"output_id": output_id, column: target_id} for output_id in output_ids]
    key = [k.c.output_id, k.c[column]]
    await conn.execute(pg_insert(k).values(rows).on_conflict_do_nothing(index_elements=key))


async def link_decision(conn: AsyncConnection, run_id: int, decision_id: int, output_ids: list[int]) -> None:
    """Have decision ``decision_id`` of run ``run_id`` carry its outputs ``output_ids``; 422 for one of another run.
    They go with the question, out of the run's notice of files no channel has tried yet."""
    wanted = await _of_run(conn, run_id, output_ids)
    await _link(conn, "decision_id", decision_id, wanted)
    await output_notices.withdraw(conn, run_id, wanted)


def _pending(run_id: int):
    """The outputs of run ``run_id`` that go under its chat messages and none carries yet: those it sent or left in
    its outputs directory."""
    o, k = tables.run_outputs, tables.run_output_links
    carried = exists().where(k.c.output_id == o.c.id, k.c.chat_message_id.is_not(None))
    return select(o.c.id).where(o.c.run_id == run_id, o.c.source != outputs.TOOL, ~carried).order_by(o.c.id)


async def _is_chat_run(conn: AsyncConnection, run_id: int) -> bool:
    r = tables.runs
    return (await conn.execute(select(r.c.kind).where(r.c.id == run_id))).scalar_one_or_none() == "chat"


async def link_message(conn: AsyncConnection, run_id: int, message_id: int, output_ids: list[int]) -> None:
    """Have chat message ``message_id``, which run ``run_id``'s agent wrote, carry its outputs ``output_ids`` (422 for
    one of another run, or of source tool) and, for a chat run, its outputs that no message carries yet."""
    wanted = await _of_run(conn, run_id, output_ids, chat=True)
    if await _is_chat_run(conn, run_id):
        wanted += [
            output_id for output_id in (await conn.execute(_pending(run_id))).scalars() if output_id not in wanted
        ]
    await _link(conn, "chat_message_id", message_id, wanted)


async def attach_leftovers(conn: AsyncConnection, run_id: int) -> None:
    """Put the outputs of chat run ``run_id`` that no message carries yet under its last message in its chat, and
    tell the chat's streams the message changed; nothing when it wrote no message there."""
    left = list((await conn.execute(_pending(run_id))).scalars())
    if not left:
        return
    r, cm = tables.runs, tables.chat_messages
    last = (
        select(cm.c.id, cm.c.chat_id)
        .join_from(cm, r, r.c.chat_id == cm.c.chat_id)
        .where(r.c.id == run_id, cm.c.run_id == run_id)
        .order_by(cm.c.seq.desc())
        .limit(1)
    )
    found = (await conn.execute(last)).one_or_none()
    if found is None:
        return
    await _link(conn, "chat_message_id", found.id, left)
    await write_chat_event(conn, found.chat_id, chats.MESSAGE_CHANGED, {"message_id": found.id})
