"""The plugin's source of its project's knowledge graph, the contribution ``documents`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): the version of each document approved last, never a draft nor a version in review.

A document is the item ``plugin.srs:document:<slug>`` of kind ``doc``, a Document of the graph, with the document's
title and label and the time its version was approved. Each heading item is a fragment, a Section, keyed by the item's
key, so a section keeps its node when it is renamed, moved or renumbered; its text is the heading's block as the
version exports it, its coded table rows included. A use case is a UseCase node and a business rule, an NFR or a common
business rule a Requirement node, with the id the link stage gives a mention of its code (``evo_agents.hub.tables_srs.
kg_node_id``: ``usecase:UC ORD-1``, ``requirement:BR ORD1-2``), so a plan step, a document or a binding that names the
code reaches the very node. The node holds its code alone: a mention of the code in a document of a lower label
shows the node at that label (``evo_agents.kg.pipeline.stages.declared``), so its title, aliases, actors and steps stay
in its section, which keeps the document's label and which search finds by them. Declared edges: the item's section
``defines`` it (a table row's heading, for a row), a business rule is ``child_of`` the use case above it, and a use
case ``mentions`` the section of each screen it uses.

A use case is a node whether or not a version of its document was approved, with its approval: ``props.approval`` is
``approved`` for a use case of the version approved last, else ``draft`` or ``in_review`` as the state of the newest
version that holds it, which is read for those use cases alone (a document with no approved version is an item with no
fragment and the nodes of its use cases). A node holds the codes it is also known by as ``props.aliases`` (the old
number of a use case, ``UC 41``), and an identifier of the source maps each alias to the code (``alias_entries``), so a
mention of either, in a commit, a pull request or a comment of the code, reaches the one node ``usecase:<code>``.

A document links its page on the hub's web (``uri``, ``document_url``) when the hub knows its public URL
(``EVO_HUB_PUBLIC_URL``), so its node and its sections' evidence lead a member from the graph to the document.

The identifiers are the template's code patterns of use cases and requirements (the config's ``code_patterns`` where
they replace them), turned into patterns that find a code inside text (``mention_pattern``).
"""

from __future__ import annotations

import re
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.srs import store
from evo_agents.hub.server.srs.doc import Document, Item
from evo_agents.hub.server.srs.markdown import block_text
from evo_agents.hub.server.srs.template import Template, heading_text
from evo_agents.hub.tables_srs import KG_NODE_KINDS, kg_node_id
from evo_agents.kg.ids import utc_text

DOCUMENT = "document"  # the kind in an item's id: plugin.srs:document:<slug>
PATTERN_CHARS = 500  # the longest pattern of an identifier (evo_agents.hub.kg_sources.MAX_PATTERN_CHARS)
ALIAS_ENTRIES = 40  # the identifiers that alias codes take, of the 50 a source may answer (MAX_IDENTIFIERS)
PENDING_STATES = ("draft", "in_review")  # the states of a version whose use cases are not approved yet


def mention_pattern(pattern: str) -> str:
    """A code pattern of a template, which matches a whole code (``^UC [A-Z]+-\\d+$``), as one that finds such a code
    inside text, between characters that cannot belong to it."""
    core = pattern[1:] if pattern.startswith("^") else pattern
    if core.endswith("$") and not core.endswith("\\$"):
        core = core[:-1]
    return rf"(?<![\w-])(?:{core})(?![\w-])"


def alias_entries(node: str, aliases: dict[str, str]) -> list[dict]:
    """The identifiers that find the aliases of the codes of node kind ``node`` (``{alias: code}``) in text, each an
    alternation of at most PATTERN_CHARS, longest alias first so a short one never hides one that starts with it; an
    alias the entries have no room for is left out."""
    found: list[dict] = []
    pattern, held = [], {}
    for alias in sorted(aliases, key=lambda a: (-len(a), a)):
        words = r"\s+".join(re.escape(word) for word in alias.split())
        if pattern and len(mention_pattern("|".join([*pattern, words]))) > PATTERN_CHARS:
            found.append({"kind": node, "pattern": mention_pattern("|".join(pattern)), "aliases": held})
            pattern, held = [], {}
        pattern.append(words)
        held[alias] = aliases[alias]
    if pattern:
        found.append({"kind": node, "pattern": mention_pattern("|".join(pattern)), "aliases": held})
    return found[:ALIAS_ENTRIES]


def identifiers(template: Template, aliases: dict[str, dict[str, str]] | None = None) -> list[dict]:
    """The identifiers of the project's codes of use cases and requirements, in the order of KG_NODE_KINDS, then those
    of their aliases (``aliases``: {node kind: {alias: code}})."""
    found = []
    for kind, node in KG_NODE_KINDS.items():
        entry = {"kind": node, "pattern": mention_pattern(template.patterns[kind].pattern)}
        if entry not in found:
            found.append(entry)
    for node, held in sorted((aliases or {}).items()):
        found += alias_entries(node, held)
    return found


def _heading_of(doc: Document, index: int) -> int:
    """The heading item whose section holds item ``index``: itself, or for a table row the heading above it."""
    at = index
    while doc.items[at].level is None and doc.items[at].parent is not None:
        at = doc.items[at].parent
    return at


def _fragments(doc: Document) -> list[dict]:
    found = [{"anchor": "_preamble", "level": 0, "text": doc.preamble}] if doc.preamble.strip() else []
    for index, item in enumerate(doc.items):
        if item.level is None:
            continue
        rows = [row for row in doc.items[index + 1 :] if row.level is None and row.parent == index]
        fragment = {
            "anchor": item.key,
            "title": heading_text(item.kind, item.title, item.code, item.fields),
            "level": item.level,
            "text": block_text(item, rows, anchors=False),
        }
        if item.parent is not None:
            fragment["parent"] = doc.items[_heading_of(doc, item.parent)].key
        found.append(fragment)
    return found


def _node(item: Item, node_id: str, anchor: str | None, approval: str) -> dict:
    """The node of a coded item: its code, its aliases (other codes) and its approval. A mention of the code in a
    document of a lower label shows the node at that label, so its title and steps stay in its section, which keeps the
    document's label. A node of a use case no version approved has no section to stay on: ``anchor`` is None."""
    props = {"code": item.code, "approval": approval}
    aliases = [" ".join(alias.split()) for alias in item.aliases if alias != item.code]
    if aliases:
        props["aliases"] = aliases
    node = {"id": node_id, "kind": KG_NODE_KINDS[item.kind], "name": item.code, "props": props}
    if anchor is not None:
        node["anchor"] = anchor
    return node


def _pending_nodes(doc: Document, known: set[str], approval: str) -> list[dict]:
    """The nodes of the use cases of a version not approved yet (``doc``) that the approved one does not hold."""
    nodes = []
    for item in doc.items:
        node_id = kg_node_id(item.kind, item.code) if item.kind == "use_case" else None
        if node_id is not None and node_id not in known:
            known.add(node_id)
            nodes.append(_node(item, node_id, None, approval))
    return nodes


def _graph(doc: Document | None, item_id: str, pending: tuple[Document, str] | None = None) -> dict:
    """The nodes and edges a version declares (see the module); ``pending`` is the newest version not approved, with
    its state, whose use cases the approved one (``doc``, None when there is none) lacks."""
    nodes, edges = [], []
    for index, item in enumerate(doc.items if doc is not None else ()):
        node_id = kg_node_id(item.kind, item.code)
        if node_id is None:
            continue
        anchor = doc.items[_heading_of(doc, index)].key
        nodes.append(_node(item, node_id, anchor, "approved"))
        edges.append({"src": f"{item_id}#{anchor}", "rel": "defines", "dst": node_id, "anchor": anchor})
        if item.kind == "business_rule":
            above = next((doc.items[at] for at in doc.ancestors(index) if doc.items[at].kind == "use_case"), None)
            target = kg_node_id(above.kind, above.code) if above is not None else None
            if target is not None:
                edges.append({"src": node_id, "rel": "child_of", "dst": target, "anchor": anchor})
        if item.kind == "use_case":
            for rel, key in item.refs:
                if rel == "uses_screen":
                    edges.append({"src": node_id, "rel": "mentions", "dst": f"{item_id}#{key}", "anchor": anchor})
    if pending is not None:
        nodes += _pending_nodes(pending[0], {node["id"] for node in nodes}, pending[1])
    return {"nodes": nodes, "edges": edges}


def document_url(base_url: str, project: str, slug: str) -> str:
    """A document's page on the hub's web (web/src/app/(app)/p/[project]/x/srs/documents/[slug])."""
    return f"{base_url.rstrip('/')}/p/{quote(project, safe='')}/x/srs/documents/{quote(slug, safe='')}"


def document_item(
    source: str,
    row: store.DocumentRow,
    snapshot: store.Snapshot | None,
    page: tuple[str, str] | None = None,
    pending: store.Snapshot | None = None,
) -> dict:
    """The kg/1 item of a document's approved version, as the hub finishes it (see the module); ``snapshot`` is None
    for a document with no approved version, whose item then has no fragment. ``pending`` is the newest version that is
    a draft or in review, read for the use cases it adds. With ``page`` (the hub's public URL and the project's name),
    its ``uri`` is the document's page on the hub's web, which its sections' evidence links to; the document's label
    covers it, so a node a lower label shows never carries it."""
    item_id = f"{source}:{DOCUMENT}:{row.slug}"
    held = snapshot or pending
    version = held.version
    decided = version.decided_at or version.created_at
    props = {"document": row.slug, "template": row.template, "srs_kind": row.kind}
    props["version"] = snapshot.version.number if snapshot else None
    waiting = (pending.doc, pending.version.state) if pending is not None else None
    item = {
        "id": item_id,
        "kind": "doc",
        "title": row.title,
        "rev_time": utc_text(decided),
        "props": {**props, "graph": _graph(snapshot.doc if snapshot else None, item_id, waiting)},
        "label": row.label,
    }
    if snapshot is not None:
        item["fragments"] = _fragments(snapshot.doc)
    if page is not None:
        item["uri"] = document_url(page[0], page[1], row.slug)
    return item


def _aliases(items: list[dict]) -> dict[str, dict[str, str]]:
    """The aliases of the nodes the items declare, {node kind: {alias: code}}."""
    found: dict[str, dict[str, str]] = {}
    for item in items:
        for node in item["props"]["graph"]["nodes"]:
            for alias in node["props"].get("aliases", ()):
                found.setdefault(node["kind"], {}).setdefault(alias, node["props"]["code"])
    return found


async def read(conn: AsyncConnection, source: str, project_id: int, project: str, base_url: str | None = None) -> dict:
    """The answer of the source: an item per document with a version, and the identifiers; each document links its
    page on the hub's web when the hub knows its public URL (``base_url``). The approved version of a document gives
    its content and the newest draft or version in review its use cases not approved yet."""
    settings = await store.project_settings(conn, project_id, project)
    page = (base_url, project) if base_url else None
    items = []
    for row in await store.kg_documents(conn, project_id):
        snapshot = None
        if row.approved_version is not None:
            snapshot = await store.snapshot(conn, await store.version(conn, row, row.approved_version))
        pending = None
        if row.latest_state in PENDING_STATES and row.latest_version > (row.approved_version or 0):
            pending = await store.snapshot(conn, await store.version(conn, row, row.latest_version))
        if snapshot is not None or pending is not None:
            items.append(document_item(source, row, snapshot, page, pending))
    return {"items": items, "identifiers": identifiers(settings.template, _aliases(items))}


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``documents``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``);
    the hub's config, when the call carries it, gives the public URL its documents' pages are at."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    base_url = getattr(context.config, "public_url", None)
    base_url = base_url if isinstance(base_url, str) else None
    async with context.engine.begin() as conn:
        project = context.project or params["project"]
        return await read(conn, params["source"], context.project_id, project, base_url)


__all__ = ["document_item", "document_url", "identifiers", "kg_source", "mention_pattern", "read"]
