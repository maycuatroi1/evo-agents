"""What the graph's full-text index holds for a node, and how ``Store.search`` reads a query against it.

SQLite's ``unicode61`` tokenizer (``remove_diacritics 2``) cuts text at anything that is not a letter or a digit, so
snake_case names and paths already come apart into words, and it folds accents: "kiểm toán" matches "kiem toan". Two
things it does not do are done here, the same way on the indexed text and on the query:

- a camelCase or PascalCase word is also indexed as its parts ("HubSession" holds "hubsession", "hub" and
  "session"; "HTTPServer" holds "http" and "server"), so a query names it either way;
- ``đ`` reads as ``d``: unicode61 keeps it as a letter of its own, so "đồ thị" would not match "do thi".

A node's text is its name, its path, and a body: the text of a section or a document, the description of a plan, a
step or a seam (not their verify commands nor their evidence), the docstring of a symbol or a module or the comment
above a definition (``kg.build.search_texts``). A body longer than ``MAX_TEXT_CHARS`` is cut there, and the build
counts the cut texts in its coverage (``search.cut``), so nothing is lost without a trace. URIs are not indexed: every
one of them shares its host and repo words.

A query is read three ways, each filling what the one before left: every word as a whole word (AND), every word as a
prefix (AND), then any word (OR), each ranked by BM25 times a light prior by kind (PRIOR). Words that only ask
(``where``, ``does``, ``the``, ``của``...) are left out of a query that has other words. ``kg_search`` then follows each
step, section or document it found to what it mentions (``with_mentions``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator

MAX_TEXT_CHARS = 100_000  # a node's body past this is cut and counted in the build's coverage
MAX_TERMS = 12  # words of a query read; the rest are dropped
WEIGHTS = (0.0, 0.0, 6.0, 2.0, 1.0)  # bm25 weights of node_fts's columns: node_id, kind, name, path, body
# A light prior on the BM25 score by kind: at the same text score, a node that is code or a document comes before one
# that describes work on it (a plan, a step, a commit, a pull request) or only groups it (a directory). The best
# RERANK_POOL matches by BM25 alone are scored again with it.
PRIOR = {
    "Plan": 0.5,
    "PlanStep": 0.5,
    "Commit": 0.5,
    "PullRequest": 0.5,
    "Directory": 0.5,
    "Section": 0.7,
    "Requirement": 0.7,
    "Chat": 0.5,  # a group chat of the hub, named by its title
    "ChatMessage": 0.5,  # a message says what was said about the work, as a commit does
    "ChatThread": 0.3,  # each thread of a chat is named by the chat's title: they would all match it
}
RERANK_POOL = 300

_FOLD = str.maketrans({"đ": "d", "Đ": "D"})
_WORD = re.compile(r"[^\W_]+")
_STOP = (
    "a an and are as at be been by can could do does did for from has have how i in into is it its of on or our "
    "should that the their there these this those to was we were what when where which who why will with would you "
    "của cho các những một trong được không với khi này thì để từ theo nào bằng lên vào và là có ở ra đã sẽ đang "
    "cũng mà nên gì đâu sao thế"
)
STOP_WORDS = frozenset(_STOP.translate(_FOLD).split())


def fold(text: str) -> str:
    """``text`` with đ read as d; the tokenizer folds every other accent itself."""
    return text.translate(_FOLD)


def camel_parts(word: str) -> list[str]:
    """The parts of a camelCase or PascalCase word, lower-cased ("getHTTPResponse": get, http, response); [] for a
    word that is not one (all lower or all upper case, or a single part)."""
    if word.islower() or word.isupper() or not any(ch.isupper() for ch in word[1:]):
        return []
    parts, start = [], 0
    for i in range(1, len(word)):
        before, here, after = word[i - 1], word[i], word[i + 1 : i + 2]
        if here.isupper() and (before.islower() or before.isdigit() or (before.isupper() and after.islower())):
            parts.append(word[start:i])
            start = i
    parts.append(word[start:])
    return [p.lower() for p in parts] if len(parts) > 1 else []


def index_text(text: str) -> str:
    """``text`` as the index holds it: folded, with the parts of its camelCase words added at the end."""
    folded = fold(text)
    extra = dict.fromkeys(part for word in _WORD.findall(folded) for part in camel_parts(word))
    return f"{folded}\n{' '.join(extra)}" if extra else folded


def body_text(parts: Iterable[str]) -> tuple[str, bool]:
    """The body of a node from its texts in order, and whether it was cut at MAX_TEXT_CHARS."""
    joined = "\n".join(p for p in parts if p)
    return joined[:MAX_TEXT_CHARS], len(joined) > MAX_TEXT_CHARS


# -- a query -----------------------------------------------------------------------------------------------------


def query_words(query: str) -> list[str]:
    """The words of a query in order, once each, as written (case kept for camelCase), without the words that only
    ask when it has others, at most MAX_TERMS."""
    words = list(dict.fromkeys(_WORD.findall(fold(query))))
    meaning = [w for w in words if w.lower() not in STOP_WORDS]
    return (meaning or words)[:MAX_TERMS]


def _quote(word: str) -> str:
    return '"' + word.lower().replace('"', '""') + '"'


def _word_match(word: str, prefix: bool) -> str:
    """One word of an AND query: the whole word, or all its camelCase parts."""
    star = "*" if prefix else ""
    parts = camel_parts(word)
    if not parts:
        return _quote(word) + star
    return f"({_quote(word)}{star} OR ({' AND '.join(_quote(p) + star for p in parts)}))"


def fts_queries(words: list[str]) -> Iterator[str]:
    """The FTS5 queries of ``words``, strictest first: all words whole, all words as prefixes, then any word or
    camelCase part of two letters or more."""
    if not words:
        return
    yield " AND ".join(_word_match(w, False) for w in words)
    yield " AND ".join(_word_match(w, True) for w in words)
    loose = dict.fromkeys(t for w in words for t in [w.lower(), *camel_parts(w)] if len(t) >= 2 or t.isdigit())
    if len(loose) > 1 or (loose and len(words) > 1):
        yield " OR ".join(_quote(t) for t in loose)


# -- what a hit mentions -----------------------------------------------------------------------------------------

MENTIONING = ("PlanStep", "Section", "Document")  # text nodes whose mentions name code and documents
MENTIONED = ("File", "Symbol", "Document", "Section")  # what a reader opens next
MENTIONS_PER_HIT = 3


def with_mentions(
    store, b: int, hits: list[dict], limit: int, visible: Callable[[tuple], bool], kinds: list[str] | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """``hits`` (visible nodes, best first) cut at ``limit``, each step, section or document followed by up to
    MENTIONS_PER_HIT files, symbols, documents and sections it mentions through visible edges, not listed yet and of
    ``kinds`` when given; and for each node so added, ``{"from": the hit's id, "label": the edge's label}``. A search
    for words a plan step or a document holds thus reaches the code they name, which is what the reader goes on to
    open (LARGER, arXiv 2605.16352: expanding from the text matches within the search)."""
    out: list[dict] = []
    via: dict[str, dict] = {}
    listed: set[str] = set()
    for hit in hits:
        if len(out) >= limit:
            break
        if hit["id"] in listed:
            continue
        out.append(hit)
        listed.add(hit["id"])
        if hit["kind"] in MENTIONING:
            for node, label in _mentioned(store, b, hit, listed, visible, kinds)[: max(0, limit - len(out))]:
                out.append(node)
                listed.add(node["id"])
                via[node["id"]] = {"from": hit["id"], "label": label}
    return out, via


def _mentioned(store, b: int, hit: dict, listed: set[str], visible, kinds) -> list[tuple[dict, tuple]]:
    """Up to MENTIONS_PER_HIT (node, edge label) that ``hit`` mentions: visible nodes of MENTIONED not in ``listed``."""
    edges = [
        e for e in store.edges_of(hit["id"], b, "out", limit=400) if e["rel"] == "mentions" and visible(e["label"])
    ]
    targets = store.nodes([e["dst"] for e in edges if e["dst"] not in listed], b)
    found: list[tuple[dict, tuple]] = []
    for e in edges:
        node = targets.get(e["dst"])
        if node is None or node["kind"] not in MENTIONED or not visible(node["label"]):
            continue
        if (kinds and node["kind"] not in kinds) or any(n["id"] == node["id"] for n, _ in found):
            continue
        found.append((node, e["label"]))
        if len(found) >= MENTIONS_PER_HIT:
            break
    return found
