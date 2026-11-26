"""The checks and decisions of the feature docs (``evo_agents.hub.server.docs``): who reads and writes the documents
of a project, which of them a reader sees, and what a save writes; the repository reads and writes the tables.

A document carries a label (the project's default, or one an admin gave it when creating it), and every version and
image of it is read under that label: through the sink X-Evo-Sink names, else the project's hub sink, as plans are
read (``ProjectAccess.visible``). A document the member cannot see answers the 404 of one that does not exist.

``save`` writes the next version of a document, creating it at version 1 when the project has none of that slug,
through ``repository.DocumentStore`` (a ``VersionedStore``: the row lock, ``base_version`` and a save that changes
nothing are its). A version's body is Markdown or, with format ``html``, a page of its own that shows no image of the
project (it embeds its images). Every image a version names must be an image of the project (``docs_images``),
uploaded before. A save that writes a version queues a build of the project's knowledge graph when it has one on the
hub (``repository.kg_documents`` is what the build reads).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import telegram
from evo_agents.hub.access import has_role
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.docs import repository
from evo_agents.hub.server.docs.models import (
    DocsDocument,
    DocsDocumentSummary,
    DocsError,
    DocsSave,
    DocsSaved,
    DocsVersion,
    DocumentRow,
    Draft,
    Saved,
    VersionRow,
    detail_model,
    summary_model,
    version_model,
)
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def visible(access: ProjectAccess, sink: str | None, label) -> bool:
    """Whether a document of ``label`` is read through ``sink`` (see ``sink_of``) with ``access``."""
    through = sink_of(access, sink)
    return through is not None and access.visible(label, through)


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise DocsError(403, f"reading the documents of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "writer"):
        raise DocsError(403, f"{doing} in project {access.name} needs the writer role on it")


def missing(project: str, slug: str) -> DocsError:
    return DocsError(404, f"no document {slug} in project {project} that you can see: see `evo-agents hub docs list`")


def cdn_allowed(access: ProjectAccess) -> bool:
    """Whether an HTML page of the project may load libraries from the CDNs of ``outputs.INTERACTIVE_POLICY``: the rule
    of an interactive answer in a chat (``telegram.restricted``), read on the project's hub sink. A project whose hub
    sink is cleared at or above the level customer, or that has none, gets no CDN."""
    sink, rules = access.rules.hub_sink, access.rules
    if sink is None:
        return False
    try:
        clearance = {"level": rules.levels[sink.level], "location": rules.locations[sink.location]}
    except (IndexError, TypeError):  # a sink the ladder does not hold: fail closed, as telegram.restricted does
        return False
    return not telegram.restricted(clearance, rules.levels, rules.locations)


# Reading


async def documents(conn: AsyncConnection, access: ProjectAccess, sink: str | None) -> list[DocumentRow]:
    """The documents of the project the member sees, by title."""
    reader(access)
    return [row for row in await repository.documents(conn, access.project_id) if visible(access, sink, row.label)]


async def document(conn: AsyncConnection, access: ProjectAccess, slug: str, sink: str | None) -> DocumentRow:
    """Document ``slug`` as the member sees it; 404 when there is none or they cannot see it."""
    reader(access)
    found = await repository.find(conn, access.project_id, slug)
    if found is None or not visible(access, sink, found.label):
        raise missing(access.name, slug)
    return found


async def version(conn: AsyncConnection, doc: DocumentRow, number: int | None = None) -> VersionRow:
    """Version ``number`` of ``doc`` (its latest when None) whole: its body and images; 404 when it has none such."""
    wanted = number if number is not None else doc.latest_version
    found = await repository.version(conn, doc.id, wanted)
    if found is None:
        raise DocsError(404, f"document {doc.slug} has no version {wanted}: it has 1 to {doc.latest_version}")
    return found


async def listed(conn: AsyncConnection, user: Principal, project: str, sink: str | None) -> list[DocsDocumentSummary]:
    """The documents of the project the caller sees, by title, each with its latest version."""
    access = await project_access(conn, user, project)
    return [summary_model(row) for row in await documents(conn, access, sink)]


async def shown(
    conn: AsyncConnection, user: Principal, project: str, slug: str, number: int | None, sink: str | None
) -> DocsDocument:
    """A document with every version, the newest first, and one version whole: the latest, or ``number``."""
    access = await project_access(conn, user, project)
    doc = await document(conn, access, slug, sink)
    detail = detail_model(project, slug, await version(conn, doc, number))
    versions = [version_model(row) for row in await repository.versions(conn, doc.id)]
    return DocsDocument(**summary_model(doc).model_dump(), versions=versions, shown=detail)


async def versions_of(
    conn: AsyncConnection, user: Principal, project: str, slug: str, sink: str | None
) -> list[DocsVersion]:
    """Every version of a document, the newest first."""
    access = await project_access(conn, user, project)
    doc = await document(conn, access, slug, sink)
    return [version_model(row) for row in await repository.versions(conn, doc.id)]


async def image_shown(
    conn: AsyncConnection, user: Principal, project: str, slug: str, sha256: str, sink: str | None
) -> tuple[str, int]:
    """The media type and size of an image a version of the document shows; 404 when none shows it."""
    access = await project_access(conn, user, project)
    doc = await document(conn, access, slug, sink)
    found = await repository.shows_image(conn, doc.id, sha256)
    if found is None:
        raise DocsError(404, f"no version of document {slug} shows image {sha256}")
    return found


async def page(
    conn: AsyncConnection, user: Principal, project: str, slug: str, number: int | None, sink: str | None
) -> tuple[str, bool]:
    """An HTML version's page and whether it may load from the CDNs; 404 for a Markdown version."""
    access = await project_access(conn, user, project)
    doc = await document(conn, access, slug, sink)
    row = await version(conn, doc, number)
    if row.format != "html":
        raise DocsError(404, f"version {row.number} of document {slug} is Markdown, not a page: read it at .../{slug}")
    return row.body or "", cdn_allowed(access)


def uploader(user: Principal, project: str) -> Callable[[AsyncConnection], Awaitable[int]]:
    """The test of who may upload an image of the project's documents (``images.upload``): the project's id."""

    async def check(conn: AsyncConnection) -> int:
        access = await project_access(conn, user, project)
        writer(access, "uploading an image of a document")
        return access.project_id

    return check


# Saving


class DocumentSave(repository.DocumentStore):
    """A save of a member: the document it writes is one they see, and an existing one takes no label."""

    def __init__(self, access: ProjectAccess, sink: str | None, label: dict | None):
        super().__init__(access.project_id, access.name)
        self.access, self.sink, self.label = access, sink, label

    async def check(self, conn: AsyncConnection, key: str, row: DocumentRow | None, base: int | None) -> None:
        if row is not None:
            if not visible(self.access, self.sink, row.label):
                raise missing(self.access.name, key)
            if self.label is not None:
                raise DocsError(422, f"document {key} exists: its label was given when it was created")
        await super().check(conn, key, row, base)


def _new_label(access: ProjectAccess, label: dict | None) -> dict:
    """The label a new document carries: the project's, or ``label`` an admin gives it, which the writer reads."""
    if label is not None and not has_role(access.role, "admin"):
        raise DocsError(403, f"only an admin of project {access.name} sets the label of a document")
    stored = access.push_label(label)
    if not visible(access, None, stored):
        raise DocsError(
            422,
            f"a new document of project {access.name} carries label {stored.get('level')}, which your grant does not "
            "read: an admin of the project creates it with a label you read",
        )
    return stored


async def _project_images(conn: AsyncConnection, access: ProjectAccess, wanted: dict[str, str]) -> None:
    """422 naming every image of ``wanted`` ({name: sha256}) that is no image of the project."""
    held = await repository.project_images(conn, access.project_id, set(wanted.values()))
    lacking = sorted(name for name, sha256 in wanted.items() if sha256 not in held)
    if lacking:
        raise DocsError(
            422,
            f"{len(lacking)} image(s) are no image of project {access.name}, such as {lacking[0]}: upload each first "
            f"(POST /v1/x/docs/{access.name}/images); nothing was saved",
        )


async def _graph_changed(conn: AsyncConnection, access: ProjectAccess) -> None:
    """Queue a build of the project's knowledge graph, whose source ``plugin.docs`` gives the latest version of each
    document (``evo_agents.hub.server.docs.kg``), in the save's transaction."""
    await queue_if_configured(JobQueue.in_transaction(), conn, access.project_id, access.name)


async def save(
    conn: AsyncConnection,
    access: ProjectAccess,
    user_id: int,
    slug: str,
    content: dict,
    images: dict[str, str],
    *,
    base_version: int | None = None,
    label: dict | None = None,
    sink: str | None = None,
) -> Saved:
    """Save ``content`` ({title, body, format, summary}) showing ``images`` ({name: sha256}) as the next version of
    document ``slug``, created when the project has none of that slug (see the module)."""
    writer(access, "saving a document")
    content = {"format": "markdown", **content}
    if content["format"] == "html" and images:
        raise DocsError(
            422,
            f"an HTML version shows no image of project {access.name}: embed each image in the page (a data: URL); "
            "nothing was saved",
        )
    await _project_images(conn, access, images)

    async def draft(row: DocumentRow | None) -> Draft:
        if row is None:
            return Draft(content, images, user_id, label=_new_label(access, label))
        return Draft(content, images, user_id, latest=await version(conn, row))

    saved = await DocumentSave(access, sink, label).save(conn, slug, base_version, draft)
    if not saved.changed:
        assert saved.before is not None
        return Saved(saved.before, saved.version, False, False)
    await _graph_changed(conn, access)
    return Saved(await document(conn, access, slug, sink), saved.version, saved.before is None, True)


async def saved(
    conn: AsyncConnection, user: Principal, project: str, slug: str, body: DocsSave, sink: str | None
) -> DocsSaved:
    """A member's save of the next version of a document, or of its version 1 (``save``)."""
    access = await project_access(conn, user, project)
    content = {"title": body.title, "body": body.body, "format": body.format, "summary": body.summary.strip()}
    images = {image.name: image.sha256 for image in body.images}
    label = body.label.model_dump(exclude_none=True) if body.label is not None else None
    done = await save(
        conn, access, user.user_id, slug, content, images, base_version=body.base_version, label=label, sink=sink
    )
    return DocsSaved(
        document=summary_model(done.document), version=done.version, created=done.created, changed=done.changed
    )
