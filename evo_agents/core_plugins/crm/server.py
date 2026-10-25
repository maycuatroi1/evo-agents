"""The routes of the plugin crm, under /v1/x/crm/<project>/ for a member with a grant on a project that enabled it (the
hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers 404).

A reader reads: ``GET /{project}/contacts?stage=&source=`` the contacts with the count of each stage, and
``GET /{project}/contacts/{login}`` one contact with its touchpoints and the stages it reached. A writer adds a note to
a contact (``POST /{project}/contacts/{login}/notes``). An admin deletes a contact on its request
(``DELETE /{project}/contacts/{login}``): the contact, its touchpoints and its history go, and the audit holds the
number of the contact and nobody's login. Reads go through the sink X-Evo-Sink names, else the project's hub sink: a
contact whose label the caller may not read is not there for them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import JSONResponse

from evo_agents.core_plugins.crm import cli_text, store
from evo_agents.core_plugins.crm.models import (
    CrmContact,
    CrmContactDetail,
    CrmContacts,
    CrmDeleted,
    CrmNoteIn,
    CrmStageReached,
    CrmTouchpoint,
)
from evo_agents.core_plugins.crm.tables import LOGIN, SOURCES, STAGES
from evo_agents.hub.server import core_plugin_api as hub

REFUSALS = {code: {"model": hub.ErrorBody} for code in (403, 404, 422)}
CRM_DELETE = "crm.delete"  # the audit action; its target is contact:<number>, never a login
ProjectName = Annotated[str, Path(pattern=hub.PROJECT_NAME)]
Login = Annotated[str, Path(pattern=LOGIN, description="the contact's GitHub login")]
Sink = Annotated[str | None, Header(alias=store.SINK_HEADER)]
StageName = Literal[*STAGES]
SourceName = Literal[*SOURCES]

Render = Annotated[
    Literal["text"] | None,
    Query(description="text: the words `evo-agents hub crm` prints, as {text}, instead of the answer"),
]


def _words(found, render: str | None, word):
    """``found``, or with ``render`` text the words of it the CLI prints (``cli_text``)."""
    if render != "text":
        return found
    return JSONResponse({"text": word(found.model_dump(mode="json"))})


router = APIRouter(tags=["plugins"], responses={401: {"model": hub.ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal (``hub.Refused``) is the hub's error body, nothing written."""
    async with hub.begin(request) as conn:
        return await work(conn)


async def _access(conn, user: hub.Session, project: str) -> tuple[hub.ProjectRef, hub.LabelReader]:
    """The caller's access to the project and the read rule of their labels there."""
    access = await hub.project_access(conn, user, project)
    store.reader(access)
    return access, await hub.label_reader(conn, store.handle(), access)


def _model(row: store.Contact) -> CrmContact:
    return CrmContact(
        login=row.login,
        profile_url=row.profile_url,
        source=row.source,
        stage=row.stage,
        first_seen_at=row.first_seen_at,
        stage_at=row.stage_at,
    )


def _missing(access: hub.ProjectRef, login: str) -> hub.Refused:
    return hub.Refused(
        404, f"no contact {login} in project {access.name} that you can see: see `evo-agents hub crm list`"
    )


async def _seen(
    conn, access: hub.ProjectRef, rule: hub.LabelReader, login: str, sink: str | None, *, lock: bool = False
) -> store.Contact:
    found = await store.contact(conn, access.id, login, lock=lock)
    if found is None or not rule.visible(found.label, sink):
        raise _missing(access, login)
    return found


@router.get("/{project}/contacts", response_model=CrmContacts)
async def list_contacts(
    request: Request,
    project: ProjectName,
    user: hub.Caller,
    stage: Annotated[StageName | None, Query(description="only contacts in this stage")] = None,
    source: Annotated[SourceName | None, Query(description="only contacts first seen from this source")] = None,
    sink: Sink = None,
    render: Render = None,
):
    """The contacts of the project and how many are in each stage."""

    async def work(conn):
        access, rule = await _access(conn, user, project)
        every = [c for c in await store.contacts(conn, access.id, limit=None) if rule.visible(c.label, sink)]
        counts = {name: sum(c.stage == name for c in every) for name in STAGES}
        shown = [c for c in every if (stage is None or c.stage == stage) and (source is None or c.source == source)]
        return CrmContacts(project=project, counts=counts, contacts=[_model(c) for c in shown[: store.MAX_LIST]])

    return _words(await _answer(request, work), render, cli_text.contacts)


@router.get("/{project}/contacts/{login}", response_model=CrmContactDetail)
async def show_contact(
    request: Request, project: ProjectName, login: Login, user: hub.Caller, sink: Sink = None, render: Render = None
):
    """One contact with its touchpoints and the stages it reached."""

    async def work(conn):
        access, rule = await _access(conn, user, project)
        found = await _seen(conn, access, rule, login, sink)
        touches = [
            CrmTouchpoint(kind=r.kind, ref=r.ref, url=r.url, note=r.note, author=r.author, happened_at=r.happened_at)
            for r in await store.touchpoints(conn, found.id)
        ]
        reached = [CrmStageReached(stage=r.stage, reached_at=r.reached_at) for r in await store.history(conn, found.id)]
        return CrmContactDetail(**_model(found).model_dump(), touchpoints=touches, history=reached)

    return _words(await _answer(request, work), render, cli_text.contact)


@router.post("/{project}/contacts/{login}/notes", status_code=201, response_model=CrmTouchpoint)
async def add_note(
    request: Request, project: ProjectName, login: Login, body: CrmNoteIn, user: hub.Caller, sink: Sink = None
):
    """A writer's note on a contact: kept as a touchpoint, deleted with the contact."""

    async def work(conn):
        access, rule = await _access(conn, user, project)
        store.writer(access, "adding a note to a contact")
        found = await _seen(conn, access, rule, login, sink)
        now = datetime.now(UTC)
        ref = uuid4().hex
        await store.add_touchpoint(conn, found.id, "note", ref, now, note=body.note, author_id=user.user_id)
        return CrmTouchpoint(kind="note", ref=ref, note=body.note, author=user.login, happened_at=now)

    return await _answer(request, work)


@router.delete("/{project}/contacts/{login}", response_model=CrmDeleted)
async def delete_contact(request: Request, project: ProjectName, login: Login, user: hub.Caller, render: Render = None):
    """Delete a contact on its request: the contact, every touchpoint and its stage history."""

    async def work(conn):
        access = await hub.project_access(conn, user, project)
        store.admin(access, "deleting a contact")
        gone = await store.delete_contact(conn, access.id, login, user.user_id)
        if gone is None:
            raise _missing(access, login)
        contact_id, held = gone
        await hub.audit(conn, store.handle(), user, CRM_DELETE, f"contact:{contact_id}", project=access)
        return CrmDeleted(deleted=True, touchpoints=held)

    return _words(await _answer(request, work), render, lambda found: cli_text.deleted(login, found))


__all__ = ["router"]
