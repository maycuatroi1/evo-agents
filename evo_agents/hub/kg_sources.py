"""The sources of a project's knowledge graph that core plugins keep in the hub's database: extension point
``kg_sources``, version 1 (``docs/plugins.md``, section Extension points).

A core plugin may contribute one source (``contributes.kg_sources``, its Python named by ``python``). Each build of
the graph of a project that enables the plugin (``evo_agents.hub.kg_build``) calls it: ``source(params, context)``,
awaited when it returns an awaitable, within SOURCE_TIMEOUT, with ``params`` ``{"source": "plugin.<plugin id>",
"project": <name>}`` and a ``CoreContext`` (the plugin, the hub's engine and config, the project). It answers
``{"items": [...], "identifiers": [...]}``:

- ``items``: kg/1 items (``evo_agents.kg.protocol``) whose ids start with ``<source>:``, each with ``kind`` and what a
  connector gives (``title``, ``body``, ``fragments``, ``uri``, ``parent``, ``props``, ``rev_time``: when its content
  last changed), and ``label``: the label of the row it came from, as the hub stores labels. ``props.graph`` declares
  nodes and edges (``evo_agents.kg.pipeline.stages.declared``). The hub gives each item its ``rev`` (its hash) and its
  kg/1 label: a level or location the knowledge config does not name reads as its highest, as the hub reads stored
  labels, and an item without a readable label, or whose label names another project in ``projects``, is left out.
  A label may name an ``audience`` (``evo_agents.kg.policy``), which the kg/1 label carries as given: only a reader
  who is every principal of it sees the item, and one the build cannot read hides the item from every reader.
- ``identifiers``: code patterns, ``{"kind", "pattern"}`` as knowledge.yaml declares them. The build puts them before
  the project's own, so a mention of such a code in any source links to the node the plugin declares for it.
- ``cursor`` (optional): what the source read up to, ``{"item", "at"}``: the id of an item of the source and when
  that item last changed, such as the newest message of the chats. The build keeps it as the source's cursor in the
  corpus, so the coverage of its graph, and kg_status, name it (``evo_agents.kg.freshness``).

The build adds each source to the knowledge config it writes into its cache (``knowledge_with``: connector
``plugin``, the lowest label, so each item's own label decides) with its identifiers, and makes the source's items in
the cached corpus those of the answer (``keep_sources``, ``evo_agents.kg.replace``): only what changed is written, an
item the plugin no longer gives is removed, and nothing at all is written when nothing changed, so the build of
unchanged data builds nothing and gives the same content hash. The items of a plugin the project no longer enables
are removed. A source that fails, or answers what version 1 does not allow, fails the build (``SourceFailure``, whose
message names no content): the project keeps its latest graph. Source ids starting with ``plugin.`` are the hub's: one
the knowledge config declares gives way to the plugin's.

Projects that enable no such plugin build exactly as before: their config and corpus are left as they are. The hub
reads one source of its own the same way, the problems of a project that has any (``kg_problems``, id
``plugin.hub_problems``, plan problem-oriented-hub), the opportunities of a project that has any
(``kg_opportunities``, id ``plugin.hub_opportunities``, plan opportunity-intake), and the decisions writers took on the
graph (``kg_decisions``, id ``plugin.hub_decisions``, plan kg-map-and-review).

A project's graph holds its group chats while it enables a core plugin whose source it grants ``chats:read`` (the
core plugin chats; ``reads_chats``): a message then queues a build (``evo_agents.hub.server.chat_store``), and the
totals of its builds, which count items most members may not read, are not shown (``evo_agents.hub.server.kg``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_decisions, kg_opportunities, kg_problems
from evo_agents.hub.plugin_calls import CoreContext, effective_grant, enabled_plugins, load
from evo_agents.hub.plugin_events import PERMISSION
from evo_agents.kg.policy import DEFAULT_LEVELS, DEFAULT_LOCATIONS
from evo_agents.kg.project import Project
from evo_agents.kg.protocol import TIME_PATTERN, finalize_item, validate_message
from evo_agents.kg.replace import FIRST_TIME, drop_sources, keep_cursor, replace_source

log = logging.getLogger(__name__)

POINT = "kg_sources"
SOURCE_PREFIX = "plugin."
CONNECTOR = "plugin"  # the connector the knowledge config names for a plugin's source
SOURCE_TIMEOUT = 300.0  # seconds a source may take to answer
ITEM_KEYS = frozenset({"id", "kind", "rev_time", "uri", "parent", "title", "body", "fragments", "props", "label"})
IDENTIFIER_KIND = re.compile(r"^[A-Z][A-Za-z0-9]*$")
MAX_IDENTIFIERS = 50  # patterns one source adds
MAX_PATTERN_CHARS = 500
CURSOR_KEYS = frozenset({"item", "at"})
HUB = "hub"  # what PluginSource.plugin names for a source of the hub's own
READS_CHATS = PERMISSION["chat.message"]  # chats:read: a plugin granted it reads the project's group chats


class SourceFailure(Exception):
    """A plugin's source that failed or answered what version 1 does not allow; the message names no content."""


@dataclass(frozen=True)
class PluginSource:
    """What a plugin's source gave a build: its id, the plugin, its items with their kg/1 labels, its identifiers, how
    many items were left out for want of a label of the project alone, and its cursor."""

    id: str
    plugin: str
    items: tuple[dict, ...]
    identifiers: tuple[dict, ...]
    left_out: int = 0
    cursor: dict | None = None


def source_id(plugin: str) -> str:
    """The id of plugin ``plugin``'s source in a project's knowledge config: ``plugin.<id>``."""
    return f"{SOURCE_PREFIX}{plugin}"


def _ladder(knowledge: dict) -> tuple[list[str], list[str]]:
    policy = knowledge.get("policy") or {}
    return list(policy.get("levels") or DEFAULT_LEVELS), list(policy.get("locations") or DEFAULT_LOCATIONS)


def kg_label(label, project: str, levels: list[str], locations: list[str]) -> dict | None:
    """The kg/1 label of a row labelled ``label`` (by names, as the hub stores labels): a level or location the ladder
    lacks reads as its highest, no location as unrestricted, integrity U unless T, and its ``audience`` as given, which
    the build reads as the highest audience, seen by no reader, when it is not a sorted list of principals. None when
    it cannot be read, or names a project besides ``project``: such a row stays out of the graph."""
    if not isinstance(label, dict):
        return None
    projects = label.get("projects", [project])
    if not isinstance(projects, list) or set(projects) != {project}:
        return None
    level = label.get("level") if label.get("level") in levels else levels[-1]
    found = {"level": level, "integrity": "T" if label.get("integrity") == "T" else "U"}
    if label.get("location") is not None:
        found["location"] = label["location"] if label["location"] in locations else locations[-1]
    if label.get("audience") not in (None, []):
        found["audience"] = label["audience"]
    return found


def _item(source: str, raw, ladder: tuple[str, ...], project: str) -> dict | None:
    """The finished kg/1 item of one item of a source's answer; None when its label keeps it out; SourceFailure when it
    is not an item of the source."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].startswith(f"{source}:"):
        raise SourceFailure(f"source {source} answered an item that is not one of its own")
    if not ITEM_KEYS.issuperset(raw):
        raise SourceFailure(f"source {source} answered an item with keys kg/1 does not allow")
    label = kg_label(raw.get("label"), project, *ladder)
    if label is None:
        return None
    item = finalize_item({**raw, "label": label, "rev_time": raw.get("rev_time") or FIRST_TIME, "rev": "-"})
    item["rev"] = item["hash"]
    if validate_message(item):
        raise SourceFailure(f"source {source} answered an item kg/1 does not allow")
    return item


def _identifiers(source: str, raw) -> tuple[dict, ...]:
    """The identifiers of a source's answer, checked: a kind as an ontology names one, a pattern that compiles and
    never matches nothing."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_IDENTIFIERS:
        raise SourceFailure(f"source {source} answered identifiers that are not a list of at most {MAX_IDENTIFIERS}")
    found: list[dict] = []
    for entry in raw:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        if not isinstance(kind, str) or not IDENTIFIER_KIND.match(kind):
            raise SourceFailure(f"source {source} answered an identifier without a kind")
        if not isinstance(pattern, str) or not 0 < len(pattern) <= MAX_PATTERN_CHARS:
            raise SourceFailure(f"source {source} answered an identifier without a pattern")
        try:
            empty = re.fullmatch(pattern, "") is not None
        except re.error:
            raise SourceFailure(f"source {source} answered a pattern that does not compile") from None
        if empty:
            raise SourceFailure(f"source {source} answered a pattern that matches nothing at all")
        if {"kind": kind, "pattern": pattern} not in found:
            found.append({"kind": kind, "pattern": pattern})
    return tuple(found)


def _cursor(source: str, raw) -> dict | None:
    """The cursor of a source's answer, checked: the id of an item of the source and a time."""
    if raw is None:
        return None
    item = raw.get("item") if isinstance(raw, dict) else None
    if not isinstance(item, str) or set(raw) != CURSOR_KEYS or not item.startswith(f"{source}:"):
        raise SourceFailure(f"source {source} answered a cursor that is not {{item, at}} of an item of its own")
    if not isinstance(raw["at"], str) or not re.match(TIME_PATTERN, raw["at"]):
        raise SourceFailure(f"source {source} answered a cursor without a time")
    return {"item": raw["item"], "at": raw["at"]}


def checked(source: str, plugin: str, answer, knowledge: dict, project: str) -> PluginSource:
    """The answer of plugin ``plugin``'s source, as a build uses it; SourceFailure when version 1 does not allow it."""
    if not isinstance(answer, dict) or not isinstance(answer.get("items"), list):
        raise SourceFailure(f"source {source} did not answer {{items, identifiers}}")
    ladder = tuple(_ladder(knowledge))
    items, ids, left_out = [], set(), 0
    for raw in answer["items"]:
        item = _item(source, raw, ladder, project)
        if item is None:
            left_out += 1
        elif item["id"] in ids:
            raise SourceFailure(f"source {source} answered two items with one id")
        else:
            ids.add(item["id"])
            items.append(item)
    identifiers = _identifiers(source, answer.get("identifiers"))
    return PluginSource(source, plugin, tuple(items), identifiers, left_out, _cursor(source, answer.get("cursor")))


async def _read(engine: AsyncEngine, config, row, item: dict, project_id: int, project: str, knowledge: dict):
    source = source_id(row.name)
    context = CoreContext(row.name, engine, config, project, project_id)
    try:
        answer = load(item["python"])({"source": source, "project": project}, context)
        if inspect.isawaitable(answer):
            answer = await asyncio.wait_for(answer, SOURCE_TIMEOUT)
    except Exception as exc:  # a core plugin's own code may raise anything; it fails the build, never the worker
        log.exception("kg source failed", extra={"plugin": row.name, "project": project})
        raise SourceFailure(f"the kg source of plugin {row.name} failed ({type(exc).__name__})") from None
    found = checked(source, row.name, answer, knowledge, project)
    if found.left_out:
        log.warning("kg source items left out", extra={"plugin": row.name, "project": project, "items": found.left_out})
    return found


async def _hub_problems(engine: AsyncEngine, config, project_id: int, project: str, knowledge: dict) -> list:
    """The hub's own source of the project's problems (``evo_agents.hub.kg_problems``), as a plugin's source is read:
    none for a project without a problem."""
    base_url = getattr(config, "public_url", None)
    async with engine.begin() as conn:
        answer = await kg_problems.read(conn, project_id, project, base_url if isinstance(base_url, str) else None)
    if not answer["items"]:
        return []
    return [checked(kg_problems.SOURCE, HUB, answer, knowledge, project)]


async def _hub_opportunities(engine: AsyncEngine, config, project_id: int, project: str, knowledge: dict) -> list:
    """The hub's own source of the project's opportunities (``evo_agents.hub.kg_opportunities``): none for a project
    without one."""
    base_url = getattr(config, "public_url", None)
    async with engine.begin() as conn:
        answer = await kg_opportunities.read(conn, project_id, project, base_url if isinstance(base_url, str) else None)
    if not answer["items"]:
        return []
    return [checked(kg_opportunities.SOURCE, HUB, answer, knowledge, project)]


async def _hub_decisions(engine: AsyncEngine, project_id: int, project: str, knowledge: dict) -> list:
    """The hub's own source of the decisions writers took on the project's graph (``evo_agents.hub.kg_decisions``):
    none for a project without an active decision."""
    async with engine.begin() as conn:
        answer = await kg_decisions.read(conn, project_id)
    if not answer["items"]:
        return []
    return [checked(kg_decisions.SOURCE, HUB, answer, knowledge, project)]


async def read_sources(engine: AsyncEngine, config, project_id: int, project: str, knowledge: dict) -> list:
    """What the source of each core plugin the project enables gives a build of its graph, by plugin name, then the
    hub's sources of the project's problems and of its decisions when it has any; raises SourceFailure."""
    async with engine.begin() as conn:
        rows = await enabled_plugins(conn, project_id)
    found = []
    for row in rows:
        contributed = (row.manifest.get("contributes") or {}).get(POINT) or []
        if row.tier == "core" and contributed and contributed[0].get("python"):
            found.append(await _read(engine, config, row, contributed[0], project_id, project, knowledge))
    found += await _hub_problems(engine, config, project_id, project, knowledge)
    found += await _hub_opportunities(engine, config, project_id, project, knowledge)
    return found + await _hub_decisions(engine, project_id, project, knowledge)


async def reads_chats(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the graph of project ``project_id`` holds its group chats: the project enables a core plugin with a
    source that it grants ``chats:read``."""
    for row in await enabled_plugins(conn, project_id):
        contributed = (row.manifest.get("contributes") or {}).get(POINT)
        if row.tier == "core" and contributed and READS_CHATS in effective_grant(row):
            return True
    return False


def knowledge_with(knowledge: dict, sources: list[PluginSource]) -> dict:
    """The knowledge config a build reads: ``knowledge`` with each plugin's source (connector ``plugin``, the lowest
    label of the ladder, so each item's own label decides) in place of any it declares under that id, and the plugins'
    identifiers before its own. ``knowledge`` itself when no plugin gives a source."""
    if not sources:
        return knowledge
    levels, locations = _ladder(knowledge)
    floor = {"level": levels[0], "location": locations[0], "integrity": "T"}
    ids = {source.id for source in sources}
    kept = [
        entry for entry in knowledge.get("sources") or [] if not (isinstance(entry, dict) and entry.get("id") in ids)
    ]
    added = [{"id": source.id, "connector": CONNECTOR, "label": floor} for source in sorted(sources, key=_by_id)]
    identifiers: list[dict] = []
    for entry in [i for source in sorted(sources, key=_by_id) for i in source.identifiers]:
        if entry not in identifiers:
            identifiers.append(dict(entry))
    for entry in knowledge.get("identifiers") or []:
        if {"kind": entry.get("kind"), "pattern": entry.get("pattern")} not in identifiers:
            identifiers.append(entry)
    found = {**knowledge, "sources": kept + added}
    if identifiers:
        found["identifiers"] = identifiers
    return found


def _by_id(source: PluginSource) -> str:
    return source.id


def keep_sources(project: Project, sources: list[PluginSource]) -> None:
    """Make each source's items and cursor in ``project``'s corpus those it gave, and remove the items of a plugin
    source no longer given (``evo_agents.kg.replace``). Blocking."""
    corpus = project.corpus()
    try:
        for source in sources:
            replace_source(corpus, source.id, list(source.items), connector=CONNECTOR, project=project.name)
            keep_cursor(corpus, source.id, source.cursor)
        keep = {source.id for source in sources}
        drop_sources(corpus, SOURCE_PREFIX, keep, connector=CONNECTOR, project=project.name)
    finally:
        corpus.close()


__all__ = [
    "CONNECTOR",
    "POINT",
    "SOURCE_PREFIX",
    "PluginSource",
    "SourceFailure",
    "checked",
    "keep_sources",
    "kg_label",
    "knowledge_with",
    "read_sources",
    "reads_chats",
    "source_id",
]
