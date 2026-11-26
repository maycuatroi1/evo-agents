"""Who reads the bytes of an attached file: the readers of a run (``views.readable_run``, by sink) a file of a message
to the run or an upload of their own not sent yet; a member of a chat who clears the label of the message that carries
it, or its uploader before it is sent; the worker that claimed a run a file of a message its inbox took, or of a chat
message the run's agent may read (``chat_read``). Anyone else gets the 404 of a file that does not exist. The bytes are
served as an output's are (``outputs.service.serving.serve``)."""

from __future__ import annotations

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.attachments import repository
from evo_agents.hub.server.attachments.service.uploads import no_file
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.repository.views import message_select
from evo_agents.hub.server.chats.service.access import chat_access, label_reader
from evo_agents.hub.server.chats.service.read import readable, run_readers
from evo_agents.hub.server.outputs.service.serving import serve
from evo_agents.hub.server.run_events import claimed_run
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal


def _found(row, attachment_id: int):
    if row is None:
        raise no_file(attachment_id)
    return row


async def of_run(
    request: Request,
    user: Principal,
    project: str,
    run_id: int,
    attachment_id: int,
    download: bool,
    range_: str | None,
    if_none_match: str | None,
    sink: str | None,
) -> Response:
    """The bytes of a file of a message to run ``run_id``, for its readers."""
    async with UnitOfWork(request.app.state.engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        row = _found(await repository.run_file(conn, attachment_id, run_id, user.user_id), attachment_id)
    return await serve(request, row, download, range_, if_none_match)


async def of_chat(
    request: Request,
    user: Principal,
    chat_id: int,
    attachment_id: int,
    download: bool,
    range_: str | None,
    if_none_match: str | None,
) -> Response:
    """The bytes of a file a message of chat ``chat_id`` carries, for a member who reads that message."""
    async with UnitOfWork(request.app.state.engine) as conn:
        await chat_access(conn, user, chat_id)
        labels = await repository.held_labels(conn, chat_id, attachment_id)
        if labels:
            reader = await label_reader(conn, user.user_id, labels)
            if not reader.sees(labels[0]):
                raise no_file(attachment_id)
            row = _found(await repository.file(conn, attachment_id), attachment_id)
        else:  # an upload not sent yet: its uploader's alone
            row = _found(await repository.unsent_file(conn, attachment_id, chat_id, user.user_id), attachment_id)
    return await serve(request, row, download, range_, if_none_match)


async def _readable_by_run(conn: AsyncConnection, user: Principal, run_id: int, attachment_id: int) -> bool:
    """Whether the agent of run ``run_id`` may read the chat message that holds attachment ``attachment_id``."""
    rows = await repository.holding_message(conn, attachment_id, message_select)
    if not rows:
        return False
    try:
        readers = await run_readers(conn, user, run_id)
    except HTTPException:
        return False
    return bool(await readable(conn, readers, rows, None))


async def of_worker(
    request: Request, user: Principal, run_id: int, attachment_id: int, range_: str | None, if_none_match: str | None
) -> Response:
    """The bytes of a file for the agent of a run the worker claimed: a file of a message its inbox took, or of a chat
    message the agent may read."""
    async with UnitOfWork(request.app.state.engine) as conn:
        await claimed_run(conn, user, run_id)
        if not await repository.in_inbox(conn, run_id, attachment_id) and not await _readable_by_run(
            conn, user, run_id, attachment_id
        ):
            raise no_file(attachment_id)
        row = _found(await repository.file(conn, attachment_id), attachment_id)
    return await serve(request, row, True, range_, if_none_match)
