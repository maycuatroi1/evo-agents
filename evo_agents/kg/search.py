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

A section, or the text of a file read whole, longer than PASSAGE_FROM characters is also indexed as passages of about
PASSAGE_CHARS (``kg.text.split_passages``: cut between paragraphs and code blocks), each a row of its own under the
node's id, named like the node, with the document's name and the headings above it before its text (contextual BM25
without a model): a question about one paragraph of a long page finds it without the rest of the page weighing on its
score. Its score is weighed by PASSAGE_WEIGHT, and a node scores its best row. A passage is no node: a hit on it is a
hit on its node, and ``Store.matched`` gives the lines of the passage that matched best, which ``kg_search`` prints.
The row is keyed ``passage_key`` in ``node_fts_rows``; a reader of before, which knows no passage, reads its hit as one
on the node.

A query is read three ways, each read while the ones before found fewer nodes than asked: every word as a whole word
(AND), every word as a prefix (AND), then any word (OR). A node scores BM25 times a light prior by kind (PRIOR) times
the weight of the way (STAGE_WEIGHTS), at its best way, and the nodes of all ways are ranked together: a node holding
every word comes first at a like score, not whatever its score, since a long page or reply that holds every word once
is often not what a question asks about. Words that only ask
(``where``, ``does``, ``the``, ``của``...) are left out of a query that has other words. The any-word query also
leaves out a word the index cannot tell from one of them once it folds accents: "án" of "Phương án" would match every
English "an". ``kg_search`` then follows each step, section or document it found to what it mentions
(``with_mentions``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator

MAX_TEXT_CHARS = 100_000  # a node's body past this is cut and counted in the build's coverage
# Words of a query read; the rest are dropped. A question often names its subject late ("does the weekly review
# suggest, as the newsletter does, that the northwind founder ..."): cut at 12, 300 news questions of some 30 words
# lost a fifth of their recall@10 against reading every word.
MAX_TERMS = 64
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
    # A message is often the only record of what was decided there, and only a reader in its chat finds it: at half,
    # any symbol or section sharing a word or two with a question about it came first.
    "ChatMessage": 1.0,
    "ChatThread": 0.9,  # named by its chat's title, which every thread of the chat shares: after a message as good
}
RERANK_POOL = 300
PASSAGE_FROM = 4_000  # a body longer than this is also indexed as passages
PASSAGE_CHARS = 1_500  # the most characters of a passage, but for a single line longer than that
# A passage's score is weighed down against its node's own row: at a like BM25, a short passage beats a long page
# that holds the words as often, which on the tune part pushed answers out of the first five at 0.9 and not at 0.75.
PASSAGE_WEIGHT = 0.75
PASSAGE_MARK = "\x1f"  # between a node's id and its passage's lines in the key of the passage's row
STAGE_WEIGHTS = (2.0, 1.5, 1.0)  # a match of all words whole, of all words as prefixes, of any word
# A document is named by its path, which seldom says what it holds: kg_search shows the passage of its body where the
# query's words gather, about SNIPPET_TOKENS words, so the reader can tell whether to open it. A section is named by
# its heading; its passage too cost a third more tokens a call on the harness's question set and found nothing more.
SNIPPET_KINDS = frozenset({"Document"})
SNIPPET_TOKENS = 16

_FOLD = str.maketrans({"đ": "d", "Đ": "D"})
_WORD = re.compile(r"[^\W_]+")
_STOP_EN = (
    "a an and are as at be been by can could do does did for from has have how i in into is it its of on or our "
    "should that the their there these this those to was we were what when where which who why will with would you"
)
_STOP_VI = (
    "của cho các những một trong được không với khi này thì để từ theo nào bằng lên vào và là có ở ra đã sẽ đang "
    "cũng mà nên gì đâu sao thế"
)
STOP_WORDS = frozenset(f"{_STOP_EN} {_STOP_VI}".translate(_FOLD).split())


def fold(text: str) -> str:
    """``text`` with đ read as d; the tokenizer folds every other accent itself."""
    return text.translate(_FOLD)


def bare(word: str) -> str:
    """``word`` as the tokenizer indexes it: lower case, folded, without accents ("Án" reads "an")."""
    decomposed = unicodedata.normalize("NFD", fold(word).lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# The stop words as the index holds them, accents folded: a query word that reads as one there ("án" is "an",
# "cua" is "của") matches every one of them, so the any-word query leaves it out; the all-words ones keep it.
BARE_STOP_WORDS = frozenset(bare(word) for word in STOP_WORDS)


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


def passage_key(node_id: str, start: int, end: int) -> str:
    """The key of a passage's row in ``node_fts_rows``: the node's id, PASSAGE_MARK, its lines (``lines 10-42``)."""
    return f"{node_id}{PASSAGE_MARK}lines {start}-{end}"


def passage_lines(key: str) -> tuple[str, str | None]:
    """(node id, the lines of the passage) of a key of ``node_fts_rows``; None for the lines of a node's own row."""
    node_id, mark, lines = key.partition(PASSAGE_MARK)
    return node_id, (lines if mark else None)


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
    camelCase part of two letters or more that the index does not read as a stop word (BARE_STOP_WORDS)."""
    if not words:
        return
    yield " AND ".join(_word_match(w, False) for w in words)
    yield " AND ".join(_word_match(w, True) for w in words)
    terms = (t for w in words for t in [w.lower(), *camel_parts(w)] if len(t) >= 2 or t.isdigit())
    loose = dict.fromkeys(t for t in terms if bare(t) not in BARE_STOP_WORDS)
    if len(loose) > 1 or (loose and len(words) > 1):
        yield " OR ".join(_quote(t) for t in loose)


_PHRASE = re.compile(r'"(?:[^"]|"")*"\*?')


def phrases(fts_query: str) -> list[str]:
    """The phrases of an FTS5 query of ``fts_queries``, once each: the quoted words, a prefix's star kept."""
    return list(dict.fromkeys(_PHRASE.findall(fts_query)))


# -- what a hit mentions -----------------------------------------------------------------------------------------

MENTIONING = ("PlanStep", "Section", "Document")  # text nodes whose mentions name code and documents
MENTIONED = ("File", "Symbol", "Document", "Section")  # what a reader opens next
MENTIONS_PER_HIT = 3


def with_mentions(
    store, b: int, hits: list[dict], limit: int, visible: Callable[[dict], bool], kinds: list[str] | None = None
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
    edges = [e for e in store.edges_of(hit["id"], b, "out", limit=400) if e["rel"] == "mentions" and visible(e)]
    targets = store.nodes([e["dst"] for e in edges if e["dst"] not in listed], b)
    found: list[tuple[dict, tuple]] = []
    for e in edges:
        node = targets.get(e["dst"])
        if node is None or node["kind"] not in MENTIONED or not visible(node):
            continue
        if (kinds and node["kind"] not in kinds) or any(n["id"] == node["id"] for n, _ in found):
            continue
        found.append((node, e["label"]))
        if len(found) >= MENTIONS_PER_HIT:
            break
    return found
