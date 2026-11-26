"""The feature's source of its project's knowledge graph, the contribution ``documents`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): the latest version of each document of the project.

A document is the item ``plugin.docs:document:<slug>`` of kind ``doc``, a Document of the graph: the title of its
latest version, its Markdown body as the item's text (an HTML page's text as ``page_text`` reads it, the markup and
scripts left out), a fragment (a Section) per heading of it as a Markdown file of a repo gives
(``evo_agents.kg.text.split_markdown``), so search finds a document by any phrase of it and the link stage
reads the codes, plans and files it mentions, and the time that version was saved. Its props are its slug
(``document``), the version's number and its author's login. Each item carries the document's own label, so a reader
whose sink does not let that label through never sees it, as on the feature's pages. Older versions are not read: a
new version replaces the item, and an image is its name in the Markdown alone.

A document may declare in the YAML block its Markdown opens with a type, links to other documents, a level and an
audience (``typed``): a declared type is a node of that kind the document defines, each link an edge of its relation,
and the item's label is the document's joined with the declared level and audience, so a document of a brand space
shows only to the members of the space's chat. What it may declare is what the project's ontology and ladder name
(``repository.kg_frame``); a document whose audience cannot be read is left out.

A document links its page on the hub's web (``uri``, ``document_url``) when the hub knows its public URL
(``EVO_HUB_PUBLIC_URL``). The cursor is the document saved last, so ``kg_status`` names how recent the source is
(``evo_agents.kg.freshness``). It adds no identifiers: a slug is no code another document names.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.docs import repository, typed
from evo_agents.hub.server.docs.models import KgDocument
from evo_agents.hub.server.docs.page_text import page_text
from evo_agents.kg.ids import utc_text
from evo_agents.kg.text import split_markdown

DOCUMENT = typed.DOCUMENT  # the kind in an item's id: plugin.docs:document:<slug>


def document_url(base_url: str, project: str, slug: str) -> str:
    """A document's page on the hub's web (web/src/app/(app)/p/[project]/x/docs/documents/[slug])."""
    return f"{base_url.rstrip('/')}/p/{quote(project, safe='')}/x/docs/documents/{quote(slug, safe='')}"


def document_item(
    source: str,
    row: KgDocument,
    page: tuple[str, str] | None = None,
    declared: tuple[typed.Declared, typed.Frame] | None = None,
    kinds: dict[str, str] | None = None,
) -> dict:
    """The kg/1 item of a document's latest version, as the hub finishes it (see the module). With ``page`` (the hub's
    public URL and the project's name), its ``uri`` is the document's page on the hub's web. With ``declared``, what
    its Markdown declares and the project's frame, the item holds the graph it declares and the label joined with it;
    ``kinds`` maps the slug of each document that declares a type to its typed node's id."""
    props: dict = {"document": row.slug, "version": row.version, "author": row.author}
    item = {
        "id": f"{source}:{DOCUMENT}:{row.slug}",
        "kind": "doc",
        "title": row.title,
        "rev_time": utc_text(row.updated_at),
        "props": props,
        "label": row.label,
    }
    text = page_text(row.body) if row.format == "html" else row.body
    if declared is not None and row.format != "html":
        found, frame = declared
        text, item["label"] = found.text, typed.label(row.label, found, frame.levels)
        graph = typed.graph(source, row.slug, row.title, found, kinds or {})
        if graph is not None:
            props["graph"] = graph
    if text.strip():
        item["body"] = {"format": "markdown", "text": text}
        item["fragments"] = split_markdown(text)
    if page is not None:
        item["uri"] = document_url(page[0], page[1], row.slug)
    return item


async def read(conn: AsyncConnection, source: str, project_id: int, project: str, base_url: str | None = None) -> dict:
    """The answer of the source: an item per document of the project, no identifiers, and as its cursor the document
    saved last; each document links its page on the hub's web when the hub knows its public URL (``base_url``)."""
    page = (base_url, project) if base_url else None
    rows = await repository.kg_documents(conn, project_id)
    frame = typed.Frame.of(*await repository.kg_frame(conn, project_id))
    found = {row.slug: typed.declared(row.body, frame) for row in rows if row.format != "html"}
    kinds = {slug: typed.node_id(source, d.kind, slug) for slug, d in found.items() if d.kind is not None}
    rows = [row for row in rows if not (row.slug in found and found[row.slug].closed)]
    items = [
        document_item(source, row, page, (found[row.slug], frame) if row.slug in found else None, kinds) for row in rows
    ]
    answer: dict = {"items": items, "identifiers": []}
    if rows:
        newest = max(rows, key=lambda row: (row.updated_at, row.slug))
        answer["cursor"] = {"item": f"{source}:{DOCUMENT}:{newest.slug}", "at": utc_text(newest.updated_at)}
    return answer


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``documents``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``);
    the hub's config, when the call carries it, gives the public URL its documents' pages are at."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    base_url = getattr(context.config, "public_url", None)
    base_url = base_url if isinstance(base_url, str) else None
    async with UnitOfWork(context.engine) as conn:
        return await read(conn, params["source"], context.project_id, context.project or params["project"], base_url)


__all__ = ["DOCUMENT", "document_item", "document_url", "kg_source", "read"]
