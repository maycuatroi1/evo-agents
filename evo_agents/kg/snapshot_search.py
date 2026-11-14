"""kg_search on an in-RAM snapshot (``evo_agents.kg.snapshot``): the ranking of ``evo_agents.kg.snapshot_text``, the
walk of ``evo_agents.kg.ppr`` when the project sets one, the reader's filter, the documents a matched entity stands
for (``search.through_entities``), what a hit mentions (``search.with_mentions``) and the files whose code holds the
query's words after the first CODE_LEAD results, as ``kg serve`` gives them (``evo_agents.kg.serve_search``).

The reader is a clearance (level, location) and an audience: a node or an edge shows when its label is at most the
clearance and its audience within the reader's (``SessionView.visible``), and a node a decision deprecated shows only
when asked for. Without a walk the store's search reads locate's arrangement (``evo_agents.kg.locate``), which reads
the store's SQL; on a snapshot the ranking keeps the order the text and meaning gave."""

from __future__ import annotations

from evo_agents.kg import ppr, semantic
from evo_agents.kg.policy import audience_below
from evo_agents.kg.refs import readable
from evo_agents.kg.search import CODE_LEAD, code_words, through_entities, with_mentions
from evo_agents.kg.snapshot import Snapshot, SnapshotView
from evo_agents.kg.snapshot_text import Ranker

SEARCH_REACH = 2000  # candidates read at most to fill a page the clearance thinned (serve_search.SEARCH_REACH)
REPO_POOL = 12  # with a repo, candidates ranked per result asked (serve_search.REPO_POOL)


def in_repo(node: dict, repo: str | None) -> bool:
    """Whether ``node`` is of ``repo`` (``serve_search.in_repo``); any node when ``repo`` is None."""
    if repo is None:
        return True
    props = node.get("props") or {}
    key = props.get("key")
    names = (
        readable(node["id"]).partition(":")[0],
        props.get("repo"),
        key[0] if isinstance(key, list) and key else None,
    )
    want = repo.lower()
    return any(isinstance(name, str) and name.lower() == want for name in names)


class SnapshotSearch:
    """kg_search of one reader on a snapshot: ``clearance`` (level, location), ``audience`` the principals it is."""

    def __init__(
        self,
        snap: Snapshot,
        clearance: tuple[int, int],
        audience: frozenset[str] = frozenset(),
        *,
        tuning=None,
        deprecated: bool = False,
        semantic_on: bool | None = None,
    ):
        from evo_agents.kg import settings as kg_settings

        self.clearance, self.audience, self.deprecated, self.semantic = clearance, audience, deprecated, semantic_on
        top = snap.top_label
        within = top[0] <= clearance[0] and top[1] <= clearance[1]
        self.sees_all = within and audience_below(snap.top_audience, audience)
        self.view: SnapshotView = snap.view(
            audience, None if self.sees_all else clearance, tuning or kg_settings.DEFAULT
        )
        self.ranker = Ranker(self.view)

    def visible(self, label: tuple) -> bool:
        if self.sees_all:
            return True
        level, location, *rest = label
        audience = rest[1] if len(rest) > 1 else frozenset()
        return level <= self.clearance[0] and location <= self.clearance[1] and audience_below(audience, self.audience)

    def seen(self, x: dict) -> bool:
        return self.visible(x["label"]) and (self.deprecated or "deprecated" not in (x.get("props") or {}))

    def search(
        self, query: str, kinds: list | None = None, limit: int = 10, repo: str | None = None
    ) -> tuple[list[dict], dict]:
        """The result nodes of kg_search, best first, and what it says of them: ``mentioned_by`` (a node a hit
        mentions: that hit), ``entities`` (a document: the matched entities it stands for), ``code`` (a file whose code
        holds the query's words: the lines that do)."""
        view, b = self.view, 0
        limit = max(1, min(int(limit), 25))
        kinds = [kinds] if isinstance(kinds, str) else kinds
        hits = self._hits(query, kinds, limit, repo)
        stood: dict[str, list[str]] = {}
        if not kinds:
            hits, stood = through_entities(view, b, hits, self.seen, view.tuning.entity_code)
            hits = [n for n in hits if in_repo(n, repo)]
        results, via = with_mentions(
            view, b, [n for n in hits if not kinds or n["kind"] in kinds], limit, self.seen, kinds
        )
        if repo:
            results = [n for n in results if in_repo(n, repo)]
            via = {nid: reached for nid, reached in via.items() if any(n["id"] == nid for n in results)}
        code: dict[str, list[int]] = {}
        if not kinds or "File" in kinds:
            results, code = self._with_code(query, results, limit, repo)
        listed = {n["id"] for n in results}
        return results, {
            "mentioned_by": {nid: reached["from"] for nid, reached in via.items()},
            "entities": {nid: why for nid, why in stood.items() if nid in listed},
            "code": {nid: lines for nid, lines in code.items() if nid in listed},
        }

    def _hits(self, query: str, kinds, limit: int, repo) -> list[dict]:
        vector = semantic.query_vector(self.view, query, self.semantic)
        ids = None if kinds or repo else self._ranked(query, limit, vector)
        if ids is None:
            pool = limit * (REPO_POOL if repo else 4)
            ids = self.ranker.search(query, 0, limit=pool, kinds=kinds, deprecated=self.deprecated, vector=vector)
        found = self.view.nodes(ids)
        hits = [found[i] for i in ids if i in found and self.seen(found[i]) and in_repo(found[i], repo)]
        if not self.sees_all and len(hits) < limit:
            known, want = set(ids), max(len(ids), limit - len(hits)) * 4
            while len(hits) < limit:
                ranked = self.ranker.search(
                    query, 0, limit=want, kinds=kinds, deprecated=self.deprecated, vector=vector
                )
                extra = [i for i in ranked if i not in known]
                known.update(extra)
                more = self.view.nodes(extra)
                hits += [more[i] for i in extra if i in more and self.seen(more[i]) and in_repo(more[i], repo)]
                if not extra or want >= SEARCH_REACH:
                    break
                want = min(want * 4, SEARCH_REACH)
        return hits

    def _ranked(self, query: str, limit: int, vector) -> list[str]:
        scored = self.ranker.search_scored(
            query, 0, limit=max(limit * 4, 30), deprecated=self.deprecated, vector=vector
        )
        walk = ppr.of(self.view.tuning.walk, self.view.tuning.entity_code)
        if walk is None:
            return [i for i, _ in scored][: limit * 12]
        return ppr.order(self.view, 0, scored, walk, self.seen)[: max(limit * 4, 40)]

    def _with_code(self, query: str, results: list[dict], limit: int, repo) -> tuple[list[dict], dict[str, list[int]]]:
        words = code_words(query)
        if not words:
            return results, {}
        listed = {n["id"] for n in results}
        found = [nid for nid, _ in self.ranker.code_files(words)]
        nodes = self.view.nodes([nid for nid in found if nid not in listed])
        extra = [nodes[nid] for nid in found if nid in nodes and self.seen(nodes[nid]) and in_repo(nodes[nid], repo)]
        merged = [*results[:CODE_LEAD], *extra, *results[CODE_LEAD:]][:limit]
        held = set(found)
        return merged, {n["id"]: self.ranker.code_lines(n["id"], words) for n in merged if n["id"] in held}


def node_json(n: dict) -> dict:
    """A result node as kg_search's structured content holds it, its label as (level, location, integrity)."""
    keys = ("id", "kind", "name", "status", "conf", "props", "aliases")
    return {**{k: n.get(k) for k in keys}, "label": list(n["label"][:3])}


class SnapshotSession:
    """A session of ``kg serve`` on a snapshot for ``evo_agents.kg.bench``: ``call`` answers kg_search as a local
    session's does, with one line per result."""

    def __init__(self, search: SnapshotSearch):
        self.searcher = search

    def call(self, method: str, args: dict) -> dict:
        if method != "kg_search":
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"error: a snapshot answers kg_search, not {method}"}],
            }
        results, said = self.searcher.search(
            args.get("query") or "", args.get("kinds"), args.get("limit", 10), args.get("repo")
        )
        lines = [f"{len(results)} result(s), snapshot {self.searcher.view.snap.content_hash[:12]}"]
        lines += [f"[{readable(n['id'])}] {n['kind']} {n['name']!r}" for n in results]
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structuredContent": {
                "results": [node_json(n) for n in results],
                **said,
                "summary": f"{len(results)} results",
            },
        }
