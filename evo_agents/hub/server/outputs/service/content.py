"""Who reads the bytes of the outputs of runs, served from the hub's own origin (``serving``) so the hub's pages show
images and video under their Content-Security-Policy:

- of a run, its readers (``views.readable_run``);
- of a decision, the decision's readers (``decisions.service.reads.readable_decision``), for an output the decision
  carries;
- of a chat, a member who reads a message of it that carries the output (its label cleared,
  ``chat_access.label_reader``); an interactive answer loads libraries from the CDNs of INTERACTIVE_POLICY unless the
  chat is restricted (``ChatAccess.restricted``, read as it serves);
- a presigned GET of the bytes in the blob store for the run's readers, an attachment of the output's ASCII name,
  working until ``expires_at``, for ``evo-agents hub run outputs --download``.

Anyone else gets the 404 of an output that does not exist.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request, Response

from evo_agents.hub import outputs
from evo_agents.hub.blobs import GET_TTL
from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.chat_access import chat_access, label_reader
from evo_agents.hub.server.decisions.service.reads import readable_decision
from evo_agents.hub.server.outputs.models import OutputLink
from evo_agents.hub.server.outputs.repository import views as repository
from evo_agents.hub.server.outputs.service.serving import serve
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal


def _no_output(output_id: int) -> NotFound:
    return NotFound(f"no output {output_id} here that you may read")


async def _output(conn, output_id: int, *where):
    row = await repository.one(conn, output_id, *where)
    if row is None:
        raise _no_output(output_id)
    return row


async def of_run(
    request: Request,
    user: Principal,
    project: str,
    run_id: int,
    output_id: int,
    download: bool,
    range_: str | None,
    if_none_match: str | None,
    sink: str | None,
) -> Response:
    """The bytes of an output of run ``run_id``, for its readers."""
    async with UnitOfWork(request.app.state.engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        row = await _output(conn, output_id, repository.of_the_run(run_id))
    return await serve(request, row, download, range_, if_none_match)


async def link(
    request: Request, user: Principal, project: str, run_id: int, output_id: int, sink: str | None
) -> OutputLink:
    """A presigned GET of the bytes of an output of run ``run_id``, as an attachment, for its readers."""
    store = blob_store(request)
    async with UnitOfWork(request.app.state.engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        row = await _output(conn, output_id, repository.of_the_run(run_id))
    expires_at = datetime.now(UTC) + GET_TTL  # taken before signing, so never later than the URL
    url = await asyncio.to_thread(store.presign_get, row.sha256, filename=outputs.download_name(row.name))
    return OutputLink(sha256=row.sha256, size=row.size, name=row.name, url=url, expires_at=expires_at)


async def of_decision(
    request: Request,
    user: Principal,
    project: str,
    decision_id: int,
    output_id: int,
    download: bool,
    range_: str | None,
    if_none_match: str | None,
    sink: str | None,
) -> Response:
    """The bytes of an output decision ``decision_id`` carries, for the decision's readers."""
    async with UnitOfWork(request.app.state.engine) as conn:
        await readable_decision(conn, user, project, decision_id, sink)
        row = await _output(conn, output_id, repository.carried_by_decision(output_id, decision_id))
    return await serve(request, row, download, range_, if_none_match)


async def of_chat(
    request: Request,
    user: Principal,
    chat_id: int,
    output_id: int,
    download: bool,
    range_: str | None,
    if_none_match: str | None,
) -> Response:
    """The bytes of an output a message of chat ``chat_id`` carries, for a member who reads that message."""
    async with UnitOfWork(request.app.state.engine) as conn:
        chat = await chat_access(conn, user, chat_id)
        found = await repository.message_labels(conn, chat_id, output_id)
        reader = await label_reader(conn, user.user_id, found)
        if not any(reader.sees(label) for label in found):
            raise _no_output(output_id)
        row = await _output(conn, output_id)
    return await serve(request, row, download, range_, if_none_match, cdn=not chat.restricted)
