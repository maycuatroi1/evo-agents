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

The identifiers are the template's code patterns of use cases and requirements (the config's ``code_patterns`` where
they replace them), turned into patterns that find a code inside text (``mention_pattern``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import store
from evo_agents.core_plugins.srs.doc import Document, Item
from evo_agents.core_plugins.srs.markdown import block_text
from evo_agents.core_plugins.srs.template import Template, heading_text
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.tables_srs import KG_NODE_KINDS, kg_node_id
from evo_agents.kg.ids import utc_text

DOCUMENT = "document"  # the kind in an item's id: plugin.srs:document:<slug>


def mention_pattern(pattern: str) -> str:
    """A code pattern of a template, which matches a whole code (``^UC [A-Z]+-\\d+$``), as one that finds such a code
    inside text, between characters that cannot belong to it."""
    core = pattern[1:] if pattern.startswith("^") else pattern
    if core.endswith("$") and not core.endswith("\\$"):
        core = core[:-1]
    return rf"(?<![\w-])(?:{core})(?![\w-])"


def identifiers(template: Template) -> list[dict]:
    """The identifiers of the project's codes of use cases and requirements, in the order of KG_NODE_KINDS."""
    found = []
    for kind, node in KG_NODE_KINDS.items():
        entry = {"kind": node, "pattern": mention_pattern(template.patterns[kind].pattern)}
        if entry not in found:
            found.append(entry)
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


def _node(item: Item, node_id: str, anchor: str) -> dict:
    """The node of a coded item: its code alone. A mention of the code in a document of a lower label shows the node
    at that label, so its title, aliases and steps stay in its section, which keeps the document's label."""
    return {
        "id": node_id,
        "kind": KG_NODE_KINDS[item.kind],
        "name": item.code,
        "props": {"code": item.code},
        "anchor": anchor,
    }


def _graph(doc: Document, item_id: str) -> dict:
    """The nodes and edges a version declares (see the module)."""
    nodes, edges = [], []
    for index, item in enumerate(doc.items):
        node_id = kg_node_id(item.kind, item.code)
        if node_id is None:
            continue
        anchor = doc.items[_heading_of(doc, index)].key
        nodes.append(_node(item, node_id, anchor))
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
    return {"nodes": nodes, "edges": edges}


def document_item(source: str, row: store.DocumentRow, snapshot: store.Snapshot) -> dict:
    """The kg/1 item of a document's approved version, as the hub finishes it (see the module)."""
    item_id = f"{source}:{DOCUMENT}:{row.slug}"
    version = snapshot.version
    decided = version.decided_at or version.created_at
    props = {"document": row.slug, "version": version.number, "template": row.template, "srs_kind": row.kind}
    return {
        "id": item_id,
        "kind": "doc",
        "title": row.title,
        "rev_time": utc_text(decided),
        "fragments": _fragments(snapshot.doc),
        "props": {**props, "graph": _graph(snapshot.doc, item_id)},
        "label": row.label,
    }


async def read(conn: AsyncConnection, source: str, project_id: int, project: str) -> dict:
    """The answer of the source: an item per document with an approved version, and the identifiers."""
    settings = await store.project_settings(conn, project_id, project)
    items = []
    for row in await store.approved_documents(conn, project_id):
        version = await store.version(conn, row, row.approved_version)
        items.append(document_item(source, row, await store.snapshot(conn, version)))
    return {"items": items, "identifiers": identifiers(settings.template)}


async def kg_source(params: dict, context: CoreContext) -> dict:
    """The contribution ``documents``: ``params`` names the source and the project (``evo_agents.hub.kg_sources``)."""
    if context.engine is None or context.project_id is None:
        return {"items": [], "identifiers": []}
    async with context.engine.begin() as conn:
        return await read(conn, params["source"], context.project_id, context.project or params["project"])


__all__ = ["document_item", "identifiers", "kg_source", "mention_pattern", "read"]
