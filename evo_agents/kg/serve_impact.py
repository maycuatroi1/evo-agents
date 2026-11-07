"""kg_impact of ``kg serve``: what depends on a set of nodes, files or the lines of a diff, walked back along the
visible edges into them, hop by hop, and the documents that mention what it reached."""

from __future__ import annotations

from evo_agents.kg.impact import Impact, coverage_line, diff_lines, path_candidates, seed_coverage
from evo_agents.kg.refs import path_like, readable
from evo_agents.kg.serve_text import SPAN, SessionText
from evo_agents.kg.serve_view import ANSWERS_NOTHING, ToolError
from evo_agents.kg.serve_walk import HUB_DEGREE, SEED_EDGES, STATUS_RANK
from evo_agents.kg.store import Store
from evo_agents.kg.tools import IMPACT_LIMIT, IMPACT_MAX_LIMIT

IMPACT_RELS = (
    "calls",
    "imports",
    "member_of",
    "defines",
    "depends_on",
    "implements",
    "verifies",
    "inherits",
    "overrides",
)
IMPACT_MIN_RANK = STATUS_RANK["resolved"]  # impact follows declared, parsed, introspected, resolved: never proposed
IMPACT_STATUSES = tuple(status for status, rank in STATUS_RANK.items() if rank >= IMPACT_MIN_RANK)
WALKED_RELS = (*IMPACT_RELS, "mentions")  # what kg_impact reads into a node: mentions name documents to re-check
IMPACT_SEED_EDGES = 2_000  # edges kg_impact reads into a seed, SEED_EDGES at a time; it reports a seed with more
IMPACT_MENTIONS_SHOWN = 3  # reached nodes a document's line names; the data has them all


class ImpactTools(SessionText):
    """kg_impact."""

    def _changed_symbols(self, store: Store, b: int, symbols: list[dict], lines: set[int]) -> list[dict]:
        """The visible symbols a set of changed lines falls in: for each line, the symbol that starts last
        at or before it (the innermost one), within its unit's line span when the unit has one."""
        extents = []
        for s in symbols:
            if not self.shows(s):
                continue
            start, end = s["props"].get("line"), None
            for w in store.where(s["id"], b):
                m = SPAN.fullmatch(w["span"] or "")
                if m:
                    start = int(m[1])
                unit = store.unit(w["unit"], b)
                m = SPAN.fullmatch((unit or {}).get("span") or "")
                if m and m[2]:
                    end = int(m[2])
            if isinstance(start, int):
                extents.append((start, end, s))
        hit: dict[str, dict] = {}
        for line in sorted(lines):
            best = None
            for start, end, s in extents:
                if start <= line and (end is None or line <= end) and (best is None or start > best[0]):
                    best = (start, s)
            if best:
                hit[best[1]["id"]] = best[1]
        return list(hit.values())

    def tool_kg_impact(
        self,
        id: str | None = None,
        ids: list | None = None,
        paths: list | None = None,
        diff: str | None = None,
        depth: int = 2,
        limit: int = IMPACT_LIMIT,
    ) -> tuple[str, dict]:
        store, b = self._store()
        found = Impact(b, max(1, min(int(depth), 3)), max(1, min(int(limit), IMPACT_MAX_LIMIT)))
        wanted = ([id] if isinstance(id, str) and id else []) + [i for i in ids or [] if isinstance(i, str)]
        paths = [p for p in ([paths] if isinstance(paths, str) else paths or []) if isinstance(p, str) and p]
        changes = diff_lines(diff) if isinstance(diff, str) else {}
        if not (wanted or paths or changes):
            raise ToolError("give an id, ids, paths (repo:path) or a unified diff")
        for nid in wanted:
            n = self._lookup(store, nid, b)
            if n is not None:
                found.seeds[n["id"]] = n
            elif path_like(nid):
                paths.append(nid)  # an id that names no node may be a path
            else:
                found.missing += 1
        if paths or changes:
            self._seeds_by_path(store, found, paths, changes)
        unmatched, found.unmatched = found.unmatched, []
        for path in unmatched:  # a path that names no file may be an id, such as one the text showed
            n = self._lookup(store, path, b)
            if n is not None:
                found.seeds[n["id"]] = n
            else:
                found.unmatched.append(path)
        if not found.seeds and not (found.ambiguous or found.unmatched):
            raise ToolError("nothing visible matches; try kg_search")
        self._walk_back(store, found)
        found.docs = {
            d: n
            for d, n in store.nodes(found.mentions, b).items()
            if self.shows(n) and d not in found.seeds and d not in found.reached
        }
        found.coverage = seed_coverage(found.seeds.values())
        return "\n".join(self._impact_lines(found)), self._impact_data(found)

    def _seeds_by_path(self, store: Store, found: Impact, paths: list[str], changes: dict[str, set[int]]) -> None:
        """Seeds from repo:path paths and the files of a diff, and the symbols its changed lines fall in. A diff
        does not say which repo it comes from: a path names a file only when exactly one visible file matches it;
        otherwise the candidates go back to the caller."""
        b = found.build
        files = [f for f in store.path_nodes(b) if self.shows(f)]
        changed: dict[str, set[int]] = {}
        for path, lines in [(p, None) for p in paths] + sorted(changes.items()):
            repo, sep, rest = path.partition(":")
            if lines is None and sep and rest:
                matches = [f for f in files if f["props"].get("repo") == repo and f["props"]["path"] == rest]
            else:
                matches = path_candidates(path, files)
            if len(matches) == 1:
                found.seeds[matches[0]["id"]] = matches[0]
                if lines:
                    changed.setdefault(matches[0]["id"], set()).update(lines)
            elif matches:
                found.ambiguous.append({"path": path, "candidates": [f["id"] for f in matches]})
            else:
                found.unmatched.append(path)
        symbols = store.file_symbols(changed, b)
        for fid, lines in changed.items():
            for s in self._changed_symbols(store, b, symbols.get(fid, []), lines):
                found.seeds[s["id"]] = s

    def _walk_back(self, store: Store, found: Impact) -> None:
        """Walk back from the seeds: whoever has an edge into a reached node depends on it. A node is reached once,
        through its strongest edge (then the first rel by name); ``rels`` lists every relation it has to that
        edge's target, so a method that both overrides and calls its parent's shows both."""
        frontier = sorted(found.seeds)
        for hop in range(found.depth + 1):
            step: dict[str, tuple] = {}
            links: dict[tuple[str, str], set[str]] = {}
            for nid in frontier:
                for e in self._edges_back(store, found, nid, hop):
                    if e["rel"] == "mentions":
                        found.mentions.setdefault(e["src"], []).append(e)
                    elif hop < found.depth and e["src"] not in found.seeds and e["src"] not in found.reached:
                        links.setdefault((e["src"], e["dst"]), set()).add(e["rel"])
                        key = (-STATUS_RANK[e["status"]], e["rel"], e["dst"])
                        if e["src"] not in step or key < step[e["src"]][0]:
                            step[e["src"]] = (key, e)
            nodes = store.nodes(step, found.build)
            frontier = []
            for other in sorted(step):
                node = nodes.get(other)
                if node is not None and self.shows(node):
                    e = step[other][1]
                    rels = sorted(links[(other, e["dst"])])
                    found.reached[other] = {"hop": hop + 1, "node": node, "edge": e, "rels": rels, "hub": False}
                    frontier.append(other)

    def _edges_back(self, store: Store, found: Impact, nid: str, hop: int) -> list[dict]:
        """The visible edges into ``nid`` that kg_impact follows, mentions included, never weaker than resolved.
        A seed's are all read, a page at a time (``_seed_edges``); a reached node with more than HUB_DEGREE edges
        is a hub: it is marked and not expanded, since half the graph depends on it."""
        if hop == 0:
            edges = self._seed_edges(store, found, nid)
        else:
            edges = store.edges_of(nid, found.build, limit=HUB_DEGREE + 1)
            if len(edges) > HUB_DEGREE:
                found.reached[nid]["hub"] = True
                return []
        return [
            e
            for e in edges
            if e["dst"] == nid
            and e["src"] != nid
            and e["rel"] in WALKED_RELS
            and STATUS_RANK.get(e["status"], 0) >= IMPACT_MIN_RANK
            and self.seen(e)
        ]

    def _seed_edges(self, store: Store, found: Impact, nid: str) -> list[dict]:
        """The edges into a seed, SEED_EDGES at a time, up to IMPACT_SEED_EDGES; a seed with more is added to
        ``found.cut`` with the number of edges it has, so no caller is dropped without a word."""
        assert self.clearance is not None, ANSWERS_NOTHING
        query = (nid, found.build, WALKED_RELS, IMPACT_STATUSES, self.clearance)
        edges: list[dict] = []
        while len(edges) < IMPACT_SEED_EDGES:
            want = min(SEED_EDGES, IMPACT_SEED_EDGES - len(edges))
            page = store.edges_into(*query, limit=want, offset=len(edges))
            edges += page
            if len(page) < want:
                return edges
        total = store.count_into(*query)
        if total > len(edges):
            found.cut.append({"seed": nid, "read": len(edges), "total": total})
        return edges

    @staticmethod
    def _impact_shown(found: Impact) -> tuple[dict[int, list[dict]], list[str]]:
        """What the text of kg_impact lists: per hop, the first ``found.limit`` dependents, strongest edge first,
        then by id; and the first ``found.doc_limit`` documents, those that mention the most reached nodes first."""
        hops: dict[int, list[dict]] = {}
        for r in found.reached.values():
            hops.setdefault(r["hop"], []).append(r)
        for group in hops.values():
            group.sort(key=lambda r: (-STATUS_RANK.get(r["edge"]["status"], 0), r["node"]["id"]))
        docs = sorted(found.docs, key=lambda d: (-len(found.mentions[d]), d))
        return {hop: group[: found.limit] for hop, group in hops.items()}, docs[: found.doc_limit]

    def _impact_lines(self, found: Impact) -> list[str]:
        hops, docs = self._impact_shown(found)
        shown = [*found.seeds.values(), *(r["node"] for g in hops.values() for r in g), *(found.docs[d] for d in docs)]
        shared, label_line = self._labels(shown)
        lines = [
            f"impact of {len(found.seeds)} seed(s) within {found.depth} hop(s) (build {found.build}):"
            f" {len(found.reached)} dependent(s), {len(found.docs)} document(s) to re-check"
        ]
        lines += label_line
        if found.seeds:
            lines.append("seeds:")
            lines += [f"  {self._line(found.seeds[s], shared)}" for s in sorted(found.seeds)]
        lines += [coverage_line(row) for row in found.coverage]
        lines += [
            f"cut: {c['total']} edges lead into [{readable(c['seed'])}] and the first {c['read']} were read; what"
            " depends on it through the others is not listed"
            for c in found.cut
        ]
        for hop in sorted(hops):
            total = sum(r["hop"] == hop for r in found.reached.values())
            lines.append(f"hop {hop}:" + (f" first {found.limit} of {total}" if total > found.limit else ""))
            for r in hops[hop]:
                e, also = r["edge"], [rel for rel in r["rels"] if rel != r["edge"]["rel"]]
                lines.append(
                    f"  {self._line(r['node'], shared)} -{e['rel']}-> [{readable(e['dst'])}] ({e['status']})"
                    + (f" (also {', '.join(also)})" if also else "")
                    + (" (hub, not expanded)" if r["hub"] else "")
                )
            if total > found.limit:
                lines.append(
                    f"  ... {total - found.limit} more at hop {hop}: raise limit (up to {IMPACT_MAX_LIMIT}), or pass "
                    "the symbols you change instead of their file"
                )
        lines += self._impact_doc_lines(found, docs, shared)
        for a in found.ambiguous:
            candidates = ", ".join(f"[{readable(c)}]" for c in a["candidates"])
            lines.append(f"ambiguous path {a['path']}: {candidates}; pass repo:path or an id")
        if found.unmatched:
            lines.append("no visible file for: " + ", ".join(found.unmatched))
        if found.missing:
            lines.append(f"{found.missing} id(s) not found or not visible")
        return lines

    def _impact_doc_lines(self, found: Impact, docs: list[str], shared: str | None) -> list[str]:
        if not found.docs:
            return []
        total = len(found.docs)
        lines = ["documents to re-check:" + (f" first {len(docs)} of {total}" if total > len(docs) else "")]
        for d in docs:
            targets = sorted(found.mentions[d], key=lambda e: e["dst"])
            more = f", and {len(targets) - IMPACT_MENTIONS_SHOWN} more" if len(targets) > IMPACT_MENTIONS_SHOWN else ""
            lines.append(
                f"  {self._line(found.docs[d], shared)} -mentions-> "
                + ", ".join(f"[{readable(e['dst'])}] ({e['status']})" for e in targets[:IMPACT_MENTIONS_SHOWN])
                + more
            )
        if total > len(docs):
            lines.append(f"  ... {total - len(docs)} more documents: raise limit (up to {IMPACT_MAX_LIMIT})")
        return lines

    def _impact_data(self, found: Impact) -> dict:
        """The structured result: every dependent and document, whatever the text lists, since callers such as the
        Curator's tier check read them all; ``left_out`` says what the text did not list."""
        cut = f"; {len(found.cut)} seed(s) cut" if found.cut else ""
        per_hop: dict[int, int] = {}
        for r in found.reached.values():
            per_hop[r["hop"]] = per_hop.get(r["hop"], 0) + 1
        return {
            "build": found.build,
            "depth": found.depth,
            "seeds": [self._node_json(found.seeds[s]) for s in sorted(found.seeds)],
            "coverage": found.coverage,
            "impacted": [
                {
                    "hop": r["hop"],
                    "node": self._node_json(r["node"]),
                    "rel": r["edge"]["rel"],
                    "rels": r["rels"],
                    "status": r["edge"]["status"],
                    "to": r["edge"]["dst"],
                    "hub": r["hub"],
                }
                for r in found.reached.values()
            ],
            "documents": [
                {
                    "node": self._node_json(found.docs[d]),
                    "mentions": [
                        {"node": e["dst"], "status": e["status"]}
                        for e in sorted(found.mentions[d], key=lambda e: e["dst"])
                    ],
                }
                for d in sorted(found.docs)
            ],
            "cut": found.cut,
            "ambiguous": found.ambiguous,
            "unmatched": found.unmatched,
            "not_found": found.missing,
            "limit": found.limit,
            "left_out": {
                "hops": {str(hop): n - found.limit for hop, n in sorted(per_hop.items()) if n > found.limit},
                "documents": max(len(found.docs) - found.doc_limit, 0),
            },
            "summary": f"{len(found.reached)} dependents, {len(found.docs)} documents{cut}",
        }
