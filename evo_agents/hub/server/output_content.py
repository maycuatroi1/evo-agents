"""The bytes of the outputs of runs (``evo_agents.hub.outputs``), served from the hub's own origin to those who may read
what lists them, so the hub's pages show images and video under their Content-Security-Policy:

- GET /v1/projects/{p}/runs/{id}/outputs/{oid}/content, to the run's readers (``views.readable_run``);
- GET /v1/projects/{p}/decisions/{id}/outputs/{oid}/content, to the decision's readers (``decisions.readable_decision``),
  for an output the decision carries;
- GET /v1/chats/{id}/outputs/{oid}/content, to a member of the chat who reads a message of it that carries the output
  (its label cleared, ``chat_access.label_reader``);
- GET /v1/projects/{p}/runs/{id}/outputs/{oid}/link, to the run's readers: a presigned GET of the bytes in the blob
  store, an attachment of the output's ASCII name, working until ``expires_at``, for ``evo-agents hub run outputs
  --download``.

Anyone else gets the 404 of an output that does not exist. A content answer carries the output's Content-Type as
``outputs.served_type`` gives it, ``nosniff``, the Content-Security-Policy of ``outputs.content_policy`` (HTML and SVG
get an opaque origin and reach nothing), ``Cross-Origin-Resource-Policy: same-origin``, a Content-Disposition with a
safe name (an attachment with ``download=true``, and always for a type no viewer of the web shows), its SHA-256 as
ETag (304 for If-None-Match), and serves one byte range of a Range header (206, or 416 for one outside the file), so a
video seeks. The bytes stream from the blob store as it reads them; a store that does not answer is 503.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, tables
from evo_agents.hub.blobs import GET_TTL, BlobStoreUnavailable
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.chat_access import chat_access, label_reader
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.decisions import readable_decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.output_views import columns
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser

OutputId = Annotated[int, Path(ge=1, le=MAX_ID)]
DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]
Sink = Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)]
Download = Annotated[bool, Query(description="answer it as an attachment, which a browser saves")]
RangeHeader = Annotated[str | None, Header(alias="Range", description="one byte range, bytes=first-last")]
IfNoneMatch = Annotated[str | None, Header(description="the ETag of the bytes the browser holds")]
CACHE = "private, max-age=3600"
CONTENT = {
    200: {"content": {"application/octet-stream": {}}, "description": "the output's bytes, in its own media type"},
    206: {"content": {"application/octet-stream": {}}, "description": "the byte range asked for"},
    304: {"description": "the bytes the browser holds, named by If-None-Match, are the output's"},
    416: {"description": "the range asked for lies outside the output"},
    **{code: {"model": ErrorBody} for code in (403, 404, 503)},
}

router = APIRouter(prefix="/v1/projects", tags=["run outputs"], responses={401: {"model": ErrorBody}})
chat_router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


class OutputLink(BaseModel):
    sha256: str
    size: int
    name: str = Field(description="the output's own name, which a download keeps")
    url: str = Field(description="a presigned GET of the bytes, working until expires_at")
    expires_at: datetime


def _no_output(output_id: int) -> HTTPException:
    return HTTPException(404, f"no output {output_id} here that you may read")


async def _output(conn: AsyncConnection, output_id: int, *where):
    o = tables.run_outputs
    row = (await conn.execute(select(*columns()).where(o.c.id == output_id, *where))).one_or_none()
    if row is None:
        raise _no_output(output_id)
    return row


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


def _headers(row, download: bool) -> dict[str, str]:
    attachment = download or not outputs.inline(row.media_type)
    return {
        "Content-Security-Policy": outputs.content_policy(row.media_type),
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "Content-Disposition": outputs.content_disposition(row.name, attachment),
        "Cache-Control": CACHE,
        "ETag": f'"{row.sha256}"',
        "Accept-Ranges": "bytes",
    }


async def serve(request: Request, row, download: bool, asked: str | None, if_none_match: str | None) -> Response:
    """The answer of output ``row``'s bytes (see the module), for a caller who may read them."""
    headers = _headers(row, download)
    if _matches(if_none_match, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    span = outputs.parse_range(asked, row.size)
    if span is False:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{row.size}"})
    first, last = span if span else (None, None)
    store = blob_store(request)
    try:
        opened = await asyncio.to_thread(store.open_range, row.sha256, first, last)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    if opened is None:
        raise HTTPException(404, f"the bytes of output {row.id} are not in the blob store")
    chunks, length = opened
    headers["Content-Length"] = str(length)
    if span:
        headers["Content-Range"] = f"bytes {first}-{last}/{row.size}"
    kind = row.media_type if download else outputs.served_type(row.media_type)
    return StreamingResponse(chunks, status_code=206 if span else 200, media_type=kind, headers=headers)


@router.get("/{project}/runs/{run_id}/outputs/{output_id}/content", response_class=Response, responses=CONTENT)
async def run_content(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    output_id: OutputId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
) -> Response:
    """The bytes of an output of the run, for its readers."""
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        row = await _output(conn, output_id, tables.run_outputs.c.run_id == run_id)
    return await serve(request, row, download, range_, if_none_match)


@router.get(
    "/{project}/runs/{run_id}/outputs/{output_id}/link",
    response_model=OutputLink,
    responses={code: {"model": ErrorBody} for code in (403, 404, 503)},
)
async def run_link(
    request: Request, project: ProjectName, run_id: RunId, output_id: OutputId, user: CurrentUser, sink: Sink = None
) -> OutputLink:
    """A presigned GET of the bytes of an output of the run, as an attachment, for its readers."""
    store = blob_store(request)
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        row = await _output(conn, output_id, tables.run_outputs.c.run_id == run_id)
    expires_at = datetime.now(UTC) + GET_TTL  # taken before signing, so never later than the URL
    url = await asyncio.to_thread(store.presign_get, row.sha256, filename=outputs.download_name(row.name))
    return OutputLink(sha256=row.sha256, size=row.size, name=row.name, url=url, expires_at=expires_at)


@router.get(
    "/{project}/decisions/{decision_id}/outputs/{output_id}/content", response_class=Response, responses=CONTENT
)
async def decision_content(
    request: Request,
    project: ProjectName,
    decision_id: DecisionId,
    output_id: OutputId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
) -> Response:
    """The bytes of an output the decision carries, for the decision's readers."""
    k = tables.run_output_links
    carried = exists().where(k.c.output_id == output_id, k.c.decision_id == decision_id)
    async with request.app.state.engine.begin() as conn:
        await readable_decision(conn, user, project, decision_id, sink)
        row = await _output(conn, output_id, carried)
    return await serve(request, row, download, range_, if_none_match)


@chat_router.get("/{chat_id}/outputs/{output_id}/content", response_class=Response, responses=CONTENT)
async def chat_content(
    request: Request,
    chat_id: ChatId,
    output_id: OutputId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of an output a message of the chat carries, for a member who reads that message."""
    k, cm = tables.run_output_links, tables.chat_messages
    labels = select(cm.c.label).join_from(k, cm, cm.c.id == k.c.chat_message_id)
    labels = labels.where(k.c.output_id == output_id, cm.c.chat_id == chat_id)
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
        found = list((await conn.execute(labels)).scalars())
        reader = await label_reader(conn, user.user_id, found)
        if not any(reader.sees(label) for label in found):
            raise _no_output(output_id)
        row = await _output(conn, output_id)
    return await serve(request, row, download, range_, if_none_match)
