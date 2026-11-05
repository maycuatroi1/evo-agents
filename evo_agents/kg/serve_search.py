"""kg_search of ``kg serve``: the visible nodes a query finds, ranked as the project says (text and meaning, the walk
of ``evo_agents.kg.ppr``, the reading of ``evo_agents.kg.locate`` or plain BM25), with the nodes they mention, the
files whose code holds its words, and the passages they matched."""

from __future__ import annotations

from collections import Counter

from evo_agents.kg import locate, ppr, semantic
from evo_agents.kg import rank as learned
from evo_agents.kg.explore import MARK, POOL
from evo_agents.kg.refs import readable
from evo_agents.kg.search import (
    CODE_LEAD,
    SNIPPET_KINDS,
    code_words,
    query_words,
    through_entities,
    with_mentions,
)
from evo_agents.kg.serve_text import SessionText
from evo_agents.kg.serve_view import ANSWERS_NOTHING
from evo_agents.kg.store import Store

FULL_LINES = 5  # the results kg_search shows whole; those past them show their id alone (kg_node reads one)
SEARCH_REACH = 2000  # how many candidates kg_search reads, at most, to fill a page the clearance thinned
REPO_POOL = 12  # with a repo, kg_search ranks this many candidates per result asked, since most are of other repos


def in_repo(node: dict, repo: str | None) -> bool:
    """Whether ``node`` is of ``repo``, a source of the project or its git repo: the source its readable id starts
    with, or the repo its props name (a file's ``repo``, a symbol's key). Any node when ``repo`` is None."""
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


def code_where(numbers: list[int]) -> str | None:
    """The lines of a code hit as a result line names them: ``line 12`` or ``lines 12, 40, 88``."""
    if not numbers:
        return None
    return f"line {numbers[0]}" if len(numbers) == 1 else "lines " + ", ".join(str(n) for n in numbers)


class SearchTools(SessionText):
    """kg_search."""

    def tool_kg_search(
        self, query: str, kinds: list | None = None, limit: int = 10, repo: str | None = None
    ) -> tuple[str, dict]:
        store, b = self._store()
        limit = max(1, min(int(limit), 25))
        kinds = [kinds] if isinstance(kinds, str) else kinds
        repo = repo.strip() if isinstance(repo, str) and repo.strip() else None
        if self.plain:
            return self._plain_search(store, b, query, kinds, limit)
        hits = self._search_hits(store, b, query, kinds, limit, repo)
        stood: dict[str, list[str]] = {}  # a source document -> the matched entities it stands for
        if not kinds:  # an entity gives its place to the documents that mention it (search.through_entities)
            hits, stood = through_entities(store, b, hits, self.seen, store.tuning.entity_code)
            hits = [n for n in hits if in_repo(n, repo)]
        results, via = with_mentions(
            store, b, [n for n in hits if not kinds or n["kind"] in kinds], limit, self.seen, kinds
        )
        if repo:  # what a hit mentions may be of another repo
            results = [n for n in results if in_repo(n, repo)]
            via = {nid: reached for nid, reached in via.items() if any(n["id"] == nid for n in results)}
        direct = [n for n in results if n["id"] not in via]  # a node a hit mentions was not found by its words
        matched = store.matched(
            [n["id"] for n in direct], query, {n["id"] for n in direct if n["kind"] in SNIPPET_KINDS}
        )
        code: dict[str, list[int]] = {}
        if not kinds or "File" in kinds:
            results, code = self._with_code(store, b, query, results, limit, repo)
        explored = self._explore(store, results, hits, via)
        for reached in via.values():
            self._saw(reached["label"])
        listed = {n["id"] for n in results}
        stood = {nid: why for nid, why in stood.items() if nid in listed}
        lines = self._search_lines(b, query, results, via, matched, code, repo, self._stood_names(store, b, stood))
        if explored is not None:
            lines.append(MARK.format(rank=explored[0], p=explored[1], ref=readable(results[explored[0] - 1]["id"])))
        return "\n".join(lines), {
            "build": b,
            "results": [self._node_json(n) for n in results],
            "mentioned_by": {nid: reached["from"] for nid, reached in via.items()},
            "entities": stood,
            "matched": matched,
            "code": {nid: found_lines for nid, found_lines in code.items() if any(n["id"] == nid for n in results)},
            "summary": f"{len(results)} results",
        }

    @staticmethod
    def _stood_names(store: Store, b: int, stood: dict[str, list[str]]) -> dict[str, str]:
        """For each result that stands for matched entities, their names as its line shows them."""
        entity = store.nodes(sorted({e for why in stood.values() for e in why}), b)
        return {nid: ", ".join(entity[e]["name"] if e in entity else e for e in why) for nid, why in stood.items()}

    def _search_hits(
        self, store: Store, b: int, query: str, kinds: list | None, limit: int, repo: str | None
    ) -> list[dict]:
        """The visible nodes of ``repo`` the ranking puts first, the node an id in ``query`` names before them, read
        further when the clearance left out some of the first."""
        vector = semantic.query_vector(store, query, self.semantic)
        ids = None if kinds or repo else self._ranked(store, b, query, limit, vector)
        if ids is None:
            pool = limit * (REPO_POOL if repo else 4)
            ids = store.search(query, b, limit=pool, kinds=kinds, deprecated=self.deprecated, vector=vector)
        named = self._lookup(store, query, b) if ":" in query else None  # an id the text showed comes first
        if named is not None:
            ids = [named["id"], *(i for i in ids if i != named["id"])]
        found = store.nodes(ids, b)
        hits = [found[i] for i in ids if i in found and self.seen(found[i]) and in_repo(found[i], repo)]
        if not self.sees_all and len(hits) < limit:  # the clearance may have left out results: read further
            more = self._seen_beyond(
                store,
                b,
                ids,
                limit - len(hits),
                lambda want: store.search(query, b, limit=want, kinds=kinds, deprecated=self.deprecated, vector=vector),
            )
            hits += [n for n in more if in_repo(n, repo)]
        return hits

    def _ranked(self, store: Store, b: int, query: str, limit: int, vector) -> list[str]:
        """The ids a search of any kind and repo ranks first: by the project's walk, else by locate's reading of the
        text and meaning matches, else in their order."""
        scored = store.search_scored(query, b, limit=max(limit * 4, 30), deprecated=self.deprecated, vector=vector)
        walk = ppr.of(store.tuning.walk, store.tuning.entity_code)
        if walk is not None:  # the project's walk ranks in place of locate's reading (evo_agents.kg.ppr)
            ids = ppr.order(store, b, scored, walk, self.seen)
        else:
            ids = locate.arrange(store, b, query, scored, ranker=learned.ranker_for(self.rank), limit=limit)
        if ids is None:
            return [i for i, _ in scored][: limit * 12]  # as many as store.search read before
        return ids[: max(limit * 4, 40)]

    def _search_lines(
        self,
        b: int,
        query: str,
        results: list[dict],
        via: dict,
        matched: dict,
        code: dict[str, list[int]],
        repo: str | None,
        stood: dict[str, str] | None = None,
    ) -> list[str]:
        """The text of kg_search: a header with the status most results have, the label line, and a line for each
        result, the first FULL_LINES whole and the others by id alone; ``stood`` names the entities a document stands
        for."""
        stood = stood or {}
        words = len(query_words(query))
        # No result holds the whole query: the line under the header says so, and the results stay at ``limit``, since
        # the evidence of a question in plain words is spread over nodes that each hold some of its words.
        partial = words > 1 and bool(matched) and all(m["words"] < words for m in matched.values())
        shared, label_line = self._labels(results)
        statuses = Counter(n["status"] for n in results)
        status = statuses.most_common(1)[0][0] if statuses else ""
        assert self.project is not None, ANSWERS_NOTHING
        head = f"{len(results)} result(s) in project {self.project.name} (build {b})"
        if statuses:
            head += f", status {status}" + ("" if len(statuses) == 1 else " unless its line names another")
        lines = [head, *label_line]
        if partial:
            lines.append(f"no result holds all {words} words of the query")
        if repo and not results:
            lines.append(f"nothing of repo {repo!r} matched: kg_status lists the sources, each with its repo")
        for rank, n in enumerate(results, 1):
            mark = "  mentions " if n["id"] in via else ""
            if rank > FULL_LINES:
                lines.append(f"{mark}[{readable(n['id'])}]")
            elif n["id"] in code:
                where = code_where(code[n["id"]])
                lines.append(
                    mark + self._line(n, shared, where, status) + (" code" if where else " code holds the words")
                )
            else:
                m = matched.get(n["id"])
                line = mark + self._line(n, shared, (m or {}).get("lines"), status)
                line += f" words={m['words']}/{words}" if m and m["words"] < words else ""
                lines.append(line + (f" entity {stood[n['id']]}" if n["id"] in stood else ""))
                if m and m.get("snippet"):
                    lines.append(f"    > {m['snippet']}")
        return lines

    def _plain_search(self, store: Store, b: int, query: str, kinds: list | None, limit: int) -> tuple[str, dict]:
        """kg_search by plain BM25: the nodes in its order, no mention, cut or exploring; the passages matched."""
        if self.plain_files:
            from evo_agents.kg import filebm25

            clone, commit = self.plain_files
            files = {
                i.split(":file:", 1)[1]: i
                for (i,) in store.db.execute("SELECT node_id FROM nodes WHERE kind = 'File' AND tx_to IS NULL")
                if ":file:" in i
            }
            ids = [files[p] for p in filebm25.rank_files(clone, commit, set(files), query, limit * 4)]
        else:
            ids = store.plain_search(query, limit * 4, kinds)
        found = store.nodes(ids, b)
        results = [found[i] for i in ids if i in found and self.seen(found[i])][:limit]
        if not self.sees_all and len(results) < limit and not self.plain_files:
            results += self._seen_beyond(
                store, b, ids, limit - len(results), lambda want: store.plain_search(query, want, kinds)
            )
        matched = store.matched(
            [n["id"] for n in results], query, {n["id"] for n in results if n["kind"] in SNIPPET_KINDS}
        )
        shared, label_line = self._labels(results)
        assert self.project is not None, ANSWERS_NOTHING
        lines = [f"{len(results)} result(s) in project {self.project.name} (build {b}), plain BM25", *label_line]
        lines += [self._line(n, shared, (matched.get(n["id"]) or {}).get("lines"), n["status"]) for n in results]
        return "\n".join(lines), {
            "build": b,
            "results": [self._node_json(n) for n in results],
            "mentioned_by": {},
            "matched": matched,
            "summary": f"{len(results)} results",
        }

    def _seen_beyond(self, store: Store, b: int, ids: list[str], wanted: int, search) -> list[dict]:
        """Up to ``wanted`` nodes the reader sees that ``search(n)`` ranks past ``ids``, reading 4 times as many
        candidates each round, up to SEARCH_REACH: the label filter runs before the cut, not after it."""
        known, out = set(ids), []
        want = max(len(ids), wanted) * 4
        while len(out) < wanted:
            ranked = search(want)
            extra = [i for i in ranked if i not in known]
            known.update(extra)
            found = store.nodes(extra, b)
            out += [found[i] for i in extra if i in found and self.seen(found[i])]
            if not extra or want >= SEARCH_REACH:
                break
            want = min(want * 4, SEARCH_REACH)
        return out[:wanted]

    def _with_code(
        self, store: Store, b: int, query: str, results: list[dict], limit: int, repo: str | None
    ) -> tuple[list[dict], dict[str, list[int]]]:
        """``results`` with the visible files whose code or markup holds the most words of ``query`` that read as a
        search of code (``search.code_words``) after its first CODE_LEAD, those already listed left in their place,
        cut at ``limit``; and the lines of each listed file that hold the words (``Store.code_lines``)."""
        words = code_words(query)
        if not words:
            return results, {}
        listed = {n["id"] for n in results}
        found = [nid for nid, _ in store.code_files(words)]
        nodes = store.nodes([nid for nid in found if nid not in listed], b)
        extra = [nodes[nid] for nid in found if nid in nodes and self.seen(nodes[nid]) and in_repo(nodes[nid], repo)]
        merged = [*results[:CODE_LEAD], *extra, *results[CODE_LEAD:]][:limit]
        held = set(found)  # a file the ranking listed already keeps its place and shows its lines too
        return merged, {n["id"]: store.code_lines(n["id"], words) for n in merged if n["id"] in held}

    def _explore(self, store: Store, results: list, hits: list, via: dict) -> tuple[int, float] | None:
        """With an explorer, maybe put a hit past ``results`` that no agent read after a search yet at rank 4 or 5 of
        them, in place, the last result dropped when that makes one more: (its rank, the chance it had), else None."""
        if self.explorer is None:
            return None
        shown = {n["id"] for n in results}
        past = [n for n in hits if n["id"] not in shown and n["id"] not in via][: 2 * POOL]
        read = store.asked_nodes(n["id"] for n in past)
        candidates = [n for n in past if n["id"] not in read]
        picked = self.explorer.pick(results, candidates)
        if picked is None:
            return None
        index, rank, chance = picked
        count = len(results)
        results.insert(rank - 1, candidates[index])
        del results[max(count, rank) :]
        kept = {n["id"] for n in results}
        for nid in [nid for nid in via if nid not in kept]:
            del via[nid]
        return rank, chance
