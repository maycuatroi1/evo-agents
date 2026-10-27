"""The rankers a core plugin contributes to the hub's searches (extension point ``rankers``, version 1).

A core plugin's manifest contributes ``rankers``: each names a Python handler and the egress sink the hits go through
(``sink``, left out when the plugin declares one). ``history_search`` (REST and MCP) and the hub's ``kg_search``
(``kg.tool_result``: the REST tools, /mcp and ``kg serve --backend hub``) call the rankers of the plugins the project
enabled, after they ordered their hits as they always did.

Only the hits the plugin's sink may read go to a ranker: the project declares the sink in its policy and the hit's
label is one ``Policy.allows`` lets that sink read, the rule of the decision API's reads. A hit the sink may not read
keeps its place, and the ones it may read take the places of the others in the order the ranker gives.

The handler is ``handler(params, context)``, awaited when it returns an awaitable (a plain function runs in a thread).
``params`` is ``{project, project_id, query, source, sink, hits}``, ``source`` ``history`` or ``kg`` and each hit
``{id, text, source}``; ``context`` is the ``CoreContext`` of the plugin in the project. It returns
``{"order": [id, ...]}``, ids of the hits it was given, each once; an id it leaves out follows the ones it named, in
the old order. Any other answer, an error or a handler that does not answer in time leaves the order as it was.

The whole search has ``BUDGET`` seconds for every ranker together. A search whose hits a ranker put in a new order
says so: ``reranked`` in the response, while each hit keeps its own ``rank`` (the ts_rank of history_search).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls
from evo_agents.hub.access import ProjectRules
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.redact import redact

log = logging.getLogger(__name__)

POINT = "rankers"
BUDGET = 2.0  # seconds for every ranker of one search together
MAX_HITS = 50  # the hits one search puts to a ranker
MAX_HIT_CHARS = 600  # the text of one hit that leaves the hub


@dataclass(frozen=True)
class RankHit:
    """A hit as a ranker reads it: an id of the search's own, its text and where it is from."""

    id: str
    text: str
    source: str


@dataclass(frozen=True)
class Ranker:
    plugin: str
    name: str
    sink: str
    handler: Callable
    project: str
    project_id: int


async def project_rankers(conn: AsyncConnection, project: str, project_id: int, rules: ProjectRules) -> list[Ranker]:
    """The rankers of the core plugins project ``project_id`` enabled, whose sink its policy declares, by plugin."""
    found: list[Ranker] = []
    for row in await plugin_calls.enabled_plugins(conn, project_id):
        if row.tier != "core":
            continue
        manifest = row.manifest
        declared = [item["id"] for item in manifest.get("egress") or ()]
        for item in (manifest.get("contributes") or {}).get(POINT) or []:
            sink = item.get("sink") or (declared[0] if len(declared) == 1 else None)
            if sink not in declared or sink not in rules.policy.sinks or sink in getattr(rules, "_closed", set()):
                continue
            try:
                handler = plugin_calls.load(item["python"])
            except Exception:  # a ranker that does not load leaves the order as it was
                log.exception("ranker does not load", extra={"plugin": row.name, "ranker": item.get("name")})
                continue
            found.append(Ranker(row.name, item["name"], sink, handler, project, project_id))
    return found


def clears(rules: ProjectRules, label: Any, sink: str) -> bool:
    """Whether the project's policy lets ``sink`` read an item carrying ``label``, as stored (None counts as the
    highest of the project): ``Policy.allows``, as the decision API's reads apply it."""
    stored = rules.stored(label) if label is not None else rules.top()
    return stored is not None and rules.policy.allows(stored, sink)


def cut(text: str) -> str:
    """``text`` redacted and cut to what one hit sends out."""
    text = redact(re.sub(r"\s+", " ", str(text))).strip()
    return text if len(text) <= MAX_HIT_CHARS else text[: MAX_HIT_CHARS - 1] + "…"


async def _answer(ranker: Ranker, params: dict, state: Any) -> Any:
    context = CoreContext(
        ranker.plugin, state.engine, state.config, ranker.project, ranker.project_id, sink=ranker.sink
    )
    if inspect.iscoroutinefunction(ranker.handler):
        return await ranker.handler(params, context)
    result = await asyncio.to_thread(ranker.handler, params, context)
    return await result if inspect.isawaitable(result) else result


def _order(answer: Any, ids: Sequence[str]) -> list[str] | None:
    """The ids ``answer`` puts in order, those it left out after them in the old order; None when it is no order of
    ``ids``."""
    named = answer.get("order") if isinstance(answer, dict) else None
    if not isinstance(named, list) or not all(isinstance(each, str) for each in named):
        return None
    if len(set(named)) != len(named) or not set(named) <= set(ids):
        return None
    left = [each for each in ids if each not in set(named)]
    return [*named, *left]


async def rerank(
    state: Any,
    rankers: Sequence[Ranker],
    query: str,
    source: str,
    hits: Sequence[RankHit],
    cleared: Mapping[str, Sequence[bool]],
) -> list[str] | None:
    """The ids of ``hits`` in the order the project's rankers give them, or None when no ranker answered in time with an
    order. ``cleared[sink][i]`` says whether hit ``i`` may go out through ``sink`` (see the module): each ranker gets
    those of its own sink, and a hit that is not cleared keeps its place."""
    ids = [hit.id for hit in hits]
    if len(hits) < 2 or not rankers or len(set(ids)) != len(ids):
        return None
    by_id = dict(zip(ids, hits, strict=True))
    current = list(ids)
    changed = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BUDGET
    for ranker in rankers:
        left = deadline - loop.time()
        if left <= 0:
            break
        ok = cleared.get(ranker.sink) or ()
        may = {each for each, yes in zip(ids, ok, strict=False) if yes}
        slots = [i for i, each in enumerate(current) if each in may][:MAX_HITS]
        sent = [current[i] for i in slots]
        if len(sent) < 2:
            continue
        params = {
            "project": ranker.project,
            "project_id": ranker.project_id,
            "query": query,
            "source": source,
            "sink": ranker.sink,
            "hits": [{"id": each, "text": by_id[each].text, "source": by_id[each].source} for each in sent],
        }
        try:
            answer = await asyncio.wait_for(_answer(ranker, params, state), timeout=left)
        except TimeoutError:
            log.warning("ranker too late", extra={"plugin": ranker.plugin, "ranker": ranker.name})
            break
        except Exception:  # a ranker that fails leaves the order as it was
            log.exception("ranker failed", extra={"plugin": ranker.plugin, "ranker": ranker.name})
            continue
        order = _order(answer, sent)
        if order is None:
            log.warning("ranker answered no order", extra={"plugin": ranker.plugin, "ranker": ranker.name})
            continue
        for slot, each in zip(slots, order, strict=True):
            current[slot] = each
        changed = True
    return current if changed else None


# kg_search


def _kg_text(node: dict, matched: Any) -> str:
    snippet = matched.get("snippet") if isinstance(matched, dict) else None
    return cut(" ".join(str(part) for part in (node.get("kind"), node.get("name"), snippet) if part))


def _reordered_text(text: str, count: int, order: Sequence[int]) -> str | None:
    """``text`` of a kg_search result with the lines of its ``count`` results, each with the snippet lines that follow it,
    in the order of ``order`` (indexes of the results as they were); None when the text does not hold them."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if not line.startswith("    >")]
    if len(starts) < count + 1 or count < 1:
        return None
    first = starts[len(starts) - count]
    if first < 1:
        return None
    blocks = [
        lines[a:b]
        for a, b in zip([*starts[len(starts) - count :]], [*starts[len(starts) - count + 1 :], len(lines)], strict=True)
    ]
    return "\n".join([*lines[:first], *(line for index in order for line in blocks[index])])


async def rerank_kg(state: Any, access: Any, query: str, result: dict) -> dict:
    """``result`` of a ``kg_search`` (``kg.tool_result``) with the results of ``structuredContent``, and the lines of the
    text that show them, in the order the rankers of the project's plugins give them, and ``reranked`` true in
    ``structuredContent``; ``reranked`` false when the project has a ranker and none changed the order, and no
    ``reranked`` when it has none. The hits
    a sink may not read are those whose label the project's policy does not let it read (``clears``)."""
    data = result.get("structuredContent")
    items = data.get("results") if isinstance(data, dict) else None
    if result.get("isError") or not isinstance(items, list) or data.get("truncated"):
        return result
    async with state.engine.begin() as conn:
        found = await project_rankers(conn, access.name, access.project_id, access.rules)
    if not found:
        return result  # a project without a ranker gets the result as it always did
    data = {**data, "reranked": False}
    result = {**result, "structuredContent": data}
    if len(items) < 2:
        return result
    matched = data.get("matched") if isinstance(data.get("matched"), dict) else {}
    hits = [RankHit(str(node.get("id")), _kg_text(node, matched.get(node.get("id"))), "kg") for node in items]
    cleared = {
        sink: [clears(access.rules, node.get("label"), sink) for node in items] for sink in {r.sink for r in found}
    }
    order = await rerank(state, found, query, "kg", hits, cleared)
    if order is None:
        return result
    where = {hit.id: index for index, hit in enumerate(hits)}
    indexes = [where[each] for each in order]
    text = result["content"][0].get("text") if result.get("content") else None
    moved = _reordered_text(text, len(items), indexes) if isinstance(text, str) else None
    if moved is None:
        return result  # the text and the data would disagree
    content = [{**result["content"][0], "text": moved}, *result["content"][1:]]
    return {
        **result,
        "content": content,
        "structuredContent": {**data, "results": [items[index] for index in indexes], "reranked": True},
    }
