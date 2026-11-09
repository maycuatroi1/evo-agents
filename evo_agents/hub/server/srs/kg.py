"""The plugin's source of its project's knowledge graph, the contribution ``documents`` to extension point kg_sources
(``evo_agents.hub.kg_sources``): the approved baseline of each document, never a draft nor a version in review. The
baseline (``baseline``) is the version approved last with each use case the reviews approved since in place of its own:
the revision of it the newest release holds (``revisions``), with the business rules under it as that revision reads; a
use case approved but absent from that version joins it under its parent heading (at the top when that heading is not
there), and one whose removal was approved leaves it. A document with no approved version but approved use cases has
those alone.

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
``approved`` for a use case of the baseline (``props.rev``, its revision there), else ``draft`` or ``in_review`` as the
state of the newest version that holds it (``in_review`` too for a revision sent to review on its own), which is read
for those use cases alone (a document with no approved version is an item with no
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
from dataclasses import replace
from datetime import datetime
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.srs import revisions, store
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


def _node(item: Item, node_id: str, anchor: str | None, approval: str, rev: int | None = None) -> dict:
    """The node of a coded item: its code, its aliases (other codes), its approval and, for a use case, its revision. A
    mention of the code in a document of a lower label shows the node at that label, so its title and steps stay in its
    section, which keeps the document's label. A node of a use case no version approved has no section to stay on:
    ``anchor`` is None."""
    props = {"code": item.code, "approval": approval}
    if rev is not None:
        props["rev"] = rev
    aliases = [" ".join(alias.split()) for alias in item.aliases if alias != item.code]
    if aliases:
        props["aliases"] = aliases
    node = {"id": node_id, "kind": KG_NODE_KINDS[item.kind], "name": item.code, "props": props}
    if anchor is not None:
        node["anchor"] = anchor
    return node


def _pending_nodes(
    doc: Document, known: set[str], approval: str, submitted: frozenset[str] = frozenset(), revs: dict | None = None
) -> list[dict]:
    """The nodes of the use cases of a version not approved yet (``doc``) that the approved one does not hold; one whose
    revision was sent to review on its own (key in ``submitted``) is in_review whatever its version is."""
    nodes = []
    for item in doc.items:
        node_id = kg_node_id(item.kind, item.code) if item.kind == "use_case" else None
        if node_id is not None and node_id not in known:
            known.add(node_id)
            state = "in_review" if item.key in submitted else approval
            nodes.append(_node(item, node_id, None, state, (revs or {}).get(item.key)))
    return nodes


def _graph(
    doc: Document | None,
    item_id: str,
    pending: tuple[Document, str] | None = None,
    revs: dict | None = None,
    submitted: frozenset[str] = frozenset(),
) -> dict:
    """The nodes and edges a version declares (see the module); ``pending`` is the newest version not approved, with
    its state, whose use cases the approved one (``doc``, None when there is none) lacks; ``revs`` the revision of each
    use case by key, the baseline's for those of ``doc``, the pending version's for the others."""
    nodes, edges = [], []
    revs = revs or {}
    for index, item in enumerate(doc.items if doc is not None else ()):
        node_id = kg_node_id(item.kind, item.code)
        if node_id is None:
            continue
        anchor = doc.items[_heading_of(doc, index)].key
        nodes.append(_node(item, node_id, anchor, "approved", revs.get(item.key) if item.kind == "use_case" else None))
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
        nodes += _pending_nodes(pending[0], {node["id"] for node in nodes}, pending[1], submitted, revs)
    return {"nodes": nodes, "edges": edges}


# The approved baseline


def _tree(doc: Document) -> list[tuple[Item, list]]:
    """``doc`` as a forest: (item, children) for each item at the top, each item copied."""
    nodes = [(replace(item, parent=None), []) for item in doc.items]
    roots = []
    for index, item in enumerate(doc.items):
        (nodes[item.parent][1] if item.parent is not None else roots).append(nodes[index])
    return roots


def _find(forest: list, key: str) -> tuple[list, int] | None:
    """The list that holds the node of item ``key`` in ``forest``, and its place in it."""
    for at, (item, children) in enumerate(forest):
        if item.key == key:
            return forest, at
        found = _find(children, key)
        if found is not None:
            return found
    return None


def _flat(forest: list) -> list[Item]:
    items: list[Item] = []

    def walk(nodes: list, parent: int | None) -> None:
        for item, children in nodes:
            items.append(replace(item, parent=parent))
            walk(children, len(items) - 1)

    walk(forest, None)
    return items


def compose(approved: Document | None, released: dict[str, tuple[Document | None, str | None]]) -> Document | None:
    """The approved baseline: ``approved`` (the version approved last, None for none) with each use case of
    ``released`` (key -> (the version its released revision first appears in, the key of its parent heading there), or
    (None, None) for a removal) in place of its own, added under its parent heading or at the top, or dropped."""
    if not released:
        return approved
    forest = _tree(approved) if approved is not None else []
    for key, (source, _) in released.items():
        held = _find(forest, key)
        if source is None:
            if held is not None:
                del held[0][held[1]]
            continue
        found = _find(_tree(source), key)
        if found is None:
            continue
        node = found[0][found[1]]
        if held is not None:
            held[0][held[1]] = node
            continue
        parent = released[key][1]
        under = _find(forest, parent) if parent else None
        (under[0][under[1]][1] if under is not None else forest).append(node)
    return Document(preamble=approved.preamble if approved is not None else "", items=_flat(forest))


async def baseline(
    conn: AsyncConnection, row: store.DocumentRow, approved: store.Snapshot | None, known: revisions.History
) -> tuple[Document | None, dict[str, int], datetime | None]:
    """The approved baseline of document ``row`` (see ``compose``), the revision of each use case in it, and when its
    newest release was made (None when it has none)."""
    sources: dict[int, Document] = {}
    released: dict[str, tuple[Document | None, str | None]] = {}
    revs: dict[str, int] = {}
    newest = None
    for key, release in known.releases.items():
        newest = release.released_at if newest is None or release.released_at > newest else newest
        if release.change == revisions.REMOVED:
            released[key] = (None, None)
            continue
        revision = next((r for r in known.revisions.get(key, ()) if r.rev == release.rev), None)
        if revision is None:
            continue
        if revision.version not in sources:
            sources[revision.version] = (
                await store.snapshot(conn, await store.version(conn, row, revision.version))
            ).doc
        source = sources[revision.version]
        at = next((index for index, item in enumerate(source.items) if item.key == key), None)
        above = source.items[at].parent if at is not None else None
        parent = source.items[above].key if above is not None else None
        released[key] = (source, parent)
        revs[key] = release.rev
    if approved is not None:
        for item in approved.doc.items:  # a use case of the version approved last with no release keeps its revision
            revision = known.at(item.key or "", approved.version.number)
            if item.kind == "use_case" and item.key and item.key not in revs and item.key not in released:
                if revision is not None:
                    revs[item.key] = revision.rev
    return compose(approved.doc if approved is not None else None, released), revs, newest


def document_url(base_url: str, project: str, slug: str) -> str:
    """A document's page on the hub's web (web/src/app/(app)/p/[project]/x/srs/documents/[slug])."""
    return f"{base_url.rstrip('/')}/p/{quote(project, safe='')}/x/srs/documents/{quote(slug, safe='')}"


def document_item(
    source: str,
    row: store.DocumentRow,
    snapshot: store.Snapshot | None,
    page: tuple[str, str] | None = None,
    pending: store.Snapshot | None = None,
    *,
    content: Document | None = None,
    revs: dict[str, int] | None = None,
    released_at: datetime | None = None,
    submitted: frozenset[str] = frozenset(),
) -> dict:
    """The kg/1 item of a document's approved baseline, as the hub finishes it (see the module); ``snapshot`` is the
    version approved last, None for a document with none, and ``content`` the baseline when use cases were approved
    since (``baseline``), else that version's content; an item with no content has no fragment. ``pending`` is the newest
    version that is a draft or in review, read for the use cases it adds; ``revs`` the revision of each use case, and
    ``submitted`` the keys of those sent to review on their own. With ``page`` (the hub's public URL and the project's
    name), its ``uri`` is the document's page on the hub's web, which its sections' evidence links to; the document's
    label covers it, so a node a lower label shows never carries it."""
    item_id = f"{source}:{DOCUMENT}:{row.slug}"
    held = snapshot or pending
    shown = content if content is not None else (snapshot.doc if snapshot else None)
    decided = (held.version.decided_at or held.version.created_at) if held is not None else released_at
    if released_at is not None and (decided is None or released_at > decided):
        decided = released_at
    props = {"document": row.slug, "template": row.template, "srs_kind": row.kind}
    props["version"] = snapshot.version.number if snapshot else None
    waiting = (pending.doc, pending.version.state) if pending is not None else None
    item = {
        "id": item_id,
        "kind": "doc",
        "title": row.title,
        "rev_time": utc_text(decided) if decided is not None else None,
        "props": {**props, "graph": _graph(shown, item_id, waiting, revs, submitted)},
        "label": row.label,
    }
    if shown is not None and shown.items:
        item["fragments"] = _fragments(shown)
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
        known = await revisions.history(conn, row.id)
        content, revs, released_at = await baseline(conn, row, snapshot, known)
        submitted = frozenset()
        if pending is not None:
            for key in known.revisions:
                revision = known.at(key, pending.version.number)
                if revision is not None and not revision.dropped:
                    revs.setdefault(key, revision.rev)
                    if revision.submitted_at is not None:
                        submitted |= {key}
        if snapshot is not None or pending is not None or content is not None:
            items.append(
                document_item(
                    source,
                    row,
                    snapshot,
                    page,
                    pending,
                    content=content,
                    revs=revs,
                    released_at=released_at,
                    submitted=submitted,
                )
            )
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


__all__ = [
    "baseline",
    "compose",
    "document_item",
    "document_url",
    "identifiers",
    "kg_source",
    "mention_pattern",
    "read",
]
