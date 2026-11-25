"""An in-RAM inverted index that scores as SQLite's FTS5 ``bm25`` does: what ``evo_agents.kg.snapshot_text`` reads
the full-text rows of a snapshot with, in place of ``node_fts``, ``node_fts_aud``, ``node_tri`` and ``code_fts``.

- Tokens are cut as FTS5's tokenizers cut them: ``unicode61 remove_diacritics 2`` keeps runs of letters and
  digits, lower-cased, accents removed (``words``); with ``tokenchars '_-$'`` (``code_fts``) those three join a word
  too (``code_words``); the trigram tokenizer (``node_tri``) reads every three characters of a text.
- A query is the subset of FTS5's syntax ``evo_agents.kg.search`` writes: quoted phrases, a phrase's last word as a
  prefix (``"word"*``), AND, OR and parentheses, AND binding tighter than OR (``parse``).
- A row's score is FTS5's: over the phrases of the query, IDF times f * (k1 + 1) / (f + k1 * (1 - b + b * D /
  avgdl)), k1 1.2 and b 0.75, f the phrase's hits in each column times the column's weight, D the row's tokens, IDF
  ln((N - n + 0.5) / (n + 0.5)) over the N rows of the table, n of which hold the phrase, or 1e-6 when that is not
  above 0. The score is negated, lower is better, as ``bm25()`` returns it."""

from __future__ import annotations

import math
import re
import unicodedata
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from evo_agents.kg.pacing import paced, paced_sorted

K1 = 1.2
B = 0.75
_WORD = re.compile(r"[^\W_]+")
_CODE_WORD = re.compile(r"[\w$-]+")
_APART = "\x00"  # between the rows of a column read as one text


@lru_cache(maxsize=1 << 16)
def _bare(token: str) -> str:
    """``token`` lower-cased, accents removed; the words of a language repeat, so each is worked out once."""
    decomposed = unicodedata.normalize("NFD", token.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    """The tokens ``unicode61 remove_diacritics 2`` cuts ``text`` into. An ASCII text is lower-cased whole, which
    moves no boundary between tokens, and cut in one call: most of a graph's text is."""
    if text.isascii():
        return _WORD.findall(text.lower())
    return [t for t in (_bare(w) if not w.isascii() else w.lower() for w in _WORD.findall(text)) if t]


def code_words(text: str) -> list[str]:
    """The tokens of ``code_fts``'s tokenizer: ``_``, ``-`` and ``$`` inside a word."""
    if text.isascii():
        return _CODE_WORD.findall(text.lower())
    return [t for t in (_bare(w) if not w.isascii() else w.lower() for w in _CODE_WORD.findall(text)) if t]


def trigrams(text: str) -> list[str]:
    """The tokens of the trigram tokenizer: every three characters of ``text``, case folded."""
    low = text.lower()
    return [low[i : i + 3] for i in range(len(low) - 2)]


# -- a query ---------------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(?P<phrase>"(?:[^"]|"")*")(?P<star>\*)?|(?P<op>AND|OR)\b|(?P<paren>[()]))')


@dataclass(frozen=True)
class Phrase:
    text: str  # as quoted, without the quotes
    prefix: bool


class QueryError(ValueError):
    """A query this index cannot read: FTS5 would fail on it too, and a search of it gives nothing."""


def parse(query: str) -> tuple[list[Phrase], tuple]:
    """The phrases of an FTS5 query in order, each as often as written, and its tree: ("p", phrase number), ("and",
    children) or ("or", children)."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(query):
        m = _TOKEN.match(query, pos)
        if m is None or m.end() == pos:
            if query[pos:].strip():
                raise QueryError(query)
            break
        pos = m.end()
        if m.group("phrase"):
            tokens.append(("p", m.group("phrase")[1:-1].replace('""', '"') + ("*" if m.group("star") else "")))
        elif m.group("op"):
            tokens.append(("op", m.group("op")))
        else:
            tokens.append(("paren", m.group("paren")))
    phrases: list[Phrase] = []
    tree, end = _or(tokens, 0, phrases)
    if end != len(tokens):
        raise QueryError(query)
    return phrases, tree


def _or(tokens, i: int, phrases: list[Phrase]) -> tuple[tuple, int]:
    parts = []
    node, i = _and(tokens, i, phrases)
    parts.append(node)
    while i < len(tokens) and tokens[i] == ("op", "OR"):
        node, i = _and(tokens, i + 1, phrases)
        parts.append(node)
    return (parts[0] if len(parts) == 1 else ("or", tuple(parts))), i


def _and(tokens, i: int, phrases: list[Phrase]) -> tuple[tuple, int]:
    parts = []
    node, i = _atom(tokens, i, phrases)
    parts.append(node)
    while i < len(tokens) and (tokens[i] == ("op", "AND") or tokens[i][0] == "p" or tokens[i] == ("paren", "(")):
        if tokens[i] == ("op", "AND"):
            i += 1
        node, i = _atom(tokens, i, phrases)
        parts.append(node)
    return (parts[0] if len(parts) == 1 else ("and", tuple(parts))), i


def _atom(tokens, i: int, phrases: list[Phrase]) -> tuple[tuple, int]:
    if i >= len(tokens):
        raise QueryError("a query that ends early")
    kind, value = tokens[i]
    if kind == "p":
        prefix = value.endswith("*")
        phrases.append(Phrase(value[:-1] if prefix else value, prefix))
        return ("p", len(phrases) - 1), i + 1
    if (kind, value) == ("paren", "("):
        node, i = _or(tokens, i + 1, phrases)
        if i >= len(tokens) or tokens[i] != ("paren", ")"):
            raise QueryError("a parenthesis left open")
        return node, i + 1
    raise QueryError(f"unexpected {value!r}")


def matching(tree: tuple, hits: Sequence[dict[int, tuple]]) -> set[int]:
    """The rows ``tree`` matches, given each phrase's hits (row: hits per column)."""
    if tree[0] == "p":
        return set(hits[tree[1]])
    parts = [matching(child, hits) for child in tree[1]]
    if tree[0] == "and":
        return set.intersection(*parts)
    return set.union(*parts)


def _cut(found: list[tuple[int, float]], k: int) -> list[tuple[int, float]]:
    """The rows of ``found`` scoring at most the ``k``-th best score, ties at it included, by row."""
    if len(found) <= k:
        return found
    if k <= 0:
        return []
    kth = sorted(s for _, s in found)[k - 1]
    return [(n, s) for n, s in found if s <= kth]


def _postings(rows, numbers, tokenize, columns: int) -> tuple[dict[str, array], array]:
    """Per token of ``rows``, one flat array of records [row, c0, c1, ...] (each row numbered by ``numbers``), and
    the tokens of each row. A dict and a tuple per (token, row) pair made the snapshot millions of objects, minutes of
    the api's CPU to build on the hub, and gigabytes to keep."""
    postings: dict[str, array] = {}
    sizes = array("I")
    for n, row in zip(numbers, paced(rows), strict=True):
        size = 0
        held: dict[str, list[int]] = {}
        for c, text in enumerate(row):
            tokens = tokenize(text or "")
            size += len(tokens)
            for t, f in Counter(tokens).items():
                counts = held.get(t)
                if counts is None:
                    counts = held[t] = [0] * columns
                counts[c] = f
        for t, counts in held.items():
            found = postings.get(t)
            if found is None:
                found = postings[t] = array("I")
            found.append(n)
            found.extend(counts)
        sizes.append(size)
    return postings, sizes


def _kept(keep, n: int) -> bool:
    return keep is None or bool(keep[n] if not callable(keep) else keep(n))


class Table:
    """The rows of one FTS5 table: per row the text of each indexed column, per token the rows and how often each
    column holds it, each row's tokens and the table's. ``tokenize`` cuts a text as the table's tokenizer does.

    The postings are one CSR over the sorted vocabulary: token ``i`` holds records ``offsets[i]`` to ``offsets[i +
    1]``, each a row (ascending) and how often each column holds the token. With numpy they are ``docs`` (int32) and
    ``counts`` (uint32, a row per record) and the search is vectorised (``wand_top_k``); without it one flat
    ``array('I')`` of records [row, c0, c1, ...] the same code reads in pure Python."""

    def __init__(self, rows: Sequence[Sequence[str]], tokenize: Callable[[str], list[str]]):
        self.rows = tuple(tuple(r) for r in paced(rows))
        self.tokenize = tokenize
        self.columns = columns = len(self.rows[0]) if self.rows else 0
        postings, sizes = _postings(self.rows, range(len(self.rows)), tokenize, columns)
        self.vocabulary = paced_sorted(postings)
        self.token_ids = {t: i for i, t in enumerate(self.vocabulary)}
        width = columns + 1
        flat, offsets = array("I"), array("q", [0])
        for t in paced(self.vocabulary):
            flat.extend(postings.pop(t))
            offsets.append(len(flat) // width)
        self.sizes = sizes
        self.total = sum(sizes)
        self.avgdl = self.total / len(self.rows) if self.rows else 0.0
        self.flat: array | None = flat
        self.offsets = offsets
        self.fast = _np is not None
        if self.fast:
            self._vectorise()
        # a trigram table's texts, joined (``_substrings``): made now, not by the first search that needs them
        self.joined: list[tuple] | None = [self._joined(c) for c in range(columns)] if tokenize is trigrams else None
        self.bounds: dict[tuple[int, int], tuple] = {}  # per token, what its block-max bounds are made of (``_parts``)

    def _vectorise(self) -> None:
        np = _np
        assert np is not None and self.flat is not None
        width = self.columns + 1
        records = np.frombuffer(self.flat, dtype=np.uint32).reshape(-1, width) if self.flat else None
        if records is None:
            self.docs = np.zeros(0, dtype=np.int32)
            self.counts = np.zeros((0, self.columns), dtype=np.uint32)
        else:
            self.docs = records[:, 0].astype(np.int32)
            self.counts = np.ascontiguousarray(records[:, 1:])
        self._normed()
        self.flat = None

    def _normed(self) -> None:
        """Each row's BM25 length norm, as ``score`` computes it: the same operations in the same order, so the same
        float64 each row gets there."""
        np = _np
        assert np is not None
        sizes = np.frombuffer(self.sizes, dtype=np.uint32).astype(np.float64) if self.sizes else np.zeros(0)
        avgdl = self.avgdl
        self.norm = K1 * (1 - B + B * sizes / avgdl) if avgdl else np.full(len(self.rows), K1 * (1 - B))

    def updated(self, rows: Sequence[Sequence[str]], old: Sequence[int]) -> Table:
        """The table of ``rows``, each of which is this table's row ``old[i]`` or, where that is -1, a new one: only
        the new rows are cut into tokens, and the postings of the others are renumbered. The table equals one made of
        ``rows``; without numpy, or when the rows kept are not in their order here, it is one."""
        if not self.fast or _np is None:
            return Table(rows, self.tokenize)
        made = _updated(self, rows, old)
        return made if made is not None else Table(rows, self.tokenize)

    def __len__(self) -> int:
        return len(self.rows)

    def _span(self, token: str) -> tuple[int, int]:
        """The records of ``token``: [start, end) of the CSR, empty when no row holds it."""
        i = self.token_ids.get(token)
        return (0, 0) if i is None else (self.offsets[i], self.offsets[i + 1])

    def _prefixed(self, prefix: str) -> tuple[int, int]:
        """The tokens starting with ``prefix``: a run [first, last) of the sorted vocabulary."""
        first = bisect_left(self.vocabulary, prefix)
        last = first
        while last < len(self.vocabulary) and self.vocabulary[last].startswith(prefix):
            last += 1
        return first, last

    def _held(self, token: str) -> dict[int, tuple]:
        """Each row holding ``token``: how often each of its columns holds it."""
        start, end = self._span(token)
        if self.fast:
            return dict(zip(self.docs[start:end].tolist(), map(tuple, self.counts[start:end].tolist()), strict=True))
        width, flat = self.columns + 1, self.flat
        assert flat is not None
        return {flat[i]: tuple(flat[i + 1 : i + width]) for i in range(start * width, end * width, width)}

    def _rows_of(self, token: str) -> list[int]:
        """The rows holding ``token``, ascending."""
        start, end = self._span(token)
        if self.fast:
            return self.docs[start:end].tolist()
        assert self.flat is not None
        width = self.columns + 1
        return self.flat[start * width : end * width : width].tolist()

    def _tokens(self, prefix: str) -> list[str]:
        first, last = self._prefixed(prefix)
        return list(self.vocabulary[first:last])

    def hits(self, phrase: Phrase) -> dict[int, tuple]:
        """Each row holding ``phrase``: how often each of its columns holds it."""
        if self.fast:
            docs, counts = _phrase_arrays(self, phrase)
            return dict(zip(docs.tolist(), map(tuple, counts.tolist()), strict=True))
        tokens = self.tokenize(phrase.text)
        if not tokens:
            return {}
        if len(tokens) == 1:
            found = self._tokens(tokens[0]) if phrase.prefix else [tokens[0]] if tokens[0] in self.token_ids else []
            out: dict[int, tuple] = {}
            for t in found:
                for n, counts in self._held(t).items():
                    have = out.get(n)
                    out[n] = counts if have is None else tuple(a + b for a, b in zip(have, counts, strict=True))
            return out
        return self._sequence(tokens, prefix=phrase.prefix, text=phrase.text)

    def count(self, phrase: Phrase) -> int:
        """How many rows hold ``phrase``: the n of its IDF."""
        return len(_phrase_arrays(self, phrase)[0]) if self.fast else len(self.hits(phrase))

    def held_by(self, phrase: Phrase, rows: Iterable[int]) -> tuple[int, dict[int, tuple]]:
        """How many rows hold ``phrase``, and the hits per column of those of ``rows`` that do."""
        if not self.fast:
            found = self.hits(phrase)
            return len(found), {n: found[n] for n in rows if n in found}
        np = _np
        assert np is not None
        docs, counts = _phrase_arrays(self, phrase)
        wanted = np.fromiter(rows, dtype=np.int64)
        at = np.searchsorted(docs, wanted)
        inside = at < len(docs)
        at, wanted = at[inside], wanted[inside]
        there = docs[at] == wanted
        at, wanted = at[there], wanted[there]
        return len(docs), dict(zip(wanted.tolist(), map(tuple, counts[at].tolist()), strict=True))

    def candidates(self, tokens: list[str], prefix: bool) -> list[int]:
        """The rows holding every token of ``tokens`` but the last, and the last (as a prefix when ``prefix``)."""
        last = self._tokens(tokens[-1]) if prefix else [tokens[-1]]
        if self.fast:
            return _candidates(self, tokens[:-1], last)
        found: set[int] | None = None
        for t in tokens[:-1]:
            rows = set(self._rows_of(t))
            found = rows if found is None else found & rows
        assert found is not None, "a phrase of several tokens has a first"
        ends: set[int] = set()
        for t in last:
            ends.update(self._rows_of(t))
        return sorted(found & ends)

    def _sequence(self, tokens: list[str], prefix: bool, text: str) -> dict:
        """The rows holding ``tokens`` one after the other in a column, the last as a prefix when ``prefix``, by
        row. A row cut into trigrams holds a run of them where its text, case folded, holds ``text`` (the trigrams
        of a query are three characters long: a prefix matches as the whole trigram): ``_substrings`` counts those
        without cutting a row."""
        sought = text.lower()
        if self.tokenize is trigrams and _APART not in sought:
            return self._substrings(sought)
        out = {}
        for n in self.candidates(tokens, prefix):
            counts = tuple(self._occurrences(self.tokenize(t or ""), tokens, prefix) for t in self.rows[n])
            if any(counts):
                out[n] = counts
        return out

    def _substrings(self, sought: str) -> dict[int, tuple]:
        """Each row whose text, case folded, holds ``sought``: how often each column does, overlaps counted. Each
        column's texts are read as one, case folded once and kept, with _APART between rows: with numpy as UTF-8
        bytes (a valid UTF-8 text starts only at a character's first byte), searched a byte at a time."""
        if self.joined is None:
            self.joined = [self._joined(c) for c in range(self.columns)]
        if self.fast:
            rows, counts = _substring_rows(self, sought.encode())
            return dict(zip(rows.tolist(), map(tuple, counts.tolist()), strict=True))
        found: dict[int, list[int]] = {}
        for c, (whole, starts) in enumerate(self.joined):
            at = whole.find(sought)
            while at >= 0:
                n = bisect_right(starts, at) - 1
                counts = found.get(n)
                if counts is None:
                    counts = found[n] = [0] * self.columns
                counts[c] += 1
                at = whole.find(sought, at + 1)
        return {n: tuple(found[n]) for n in sorted(found)}

    def _joined(self, column: int) -> tuple:
        texts = [(row[column] or "").lower() for row in self.rows]
        if self.fast:
            np = _np
            assert np is not None
            encoded = [t.encode() for t in texts]
            starts = np.cumsum([0] + [len(t) + 1 for t in encoded[:-1]], dtype=np.int64) if encoded else np.zeros(0)
            return np.frombuffer(_APART.encode().join(encoded), dtype=np.uint8), starts
        starts, at = array("q"), 0
        for t in texts:
            starts.append(at)
            at += len(t) + 1
        return _APART.join(texts), starts

    @staticmethod
    def _occurrences(held: list[str], tokens: list[str], prefix: bool) -> int:
        count, width, first = 0, len(tokens), tokens[0]
        for i in range(len(held) - width + 1):
            if held[i] != first:
                continue
            window = held[i : i + width]
            if window[:-1] == tokens[:-1] and (
                window[-1].startswith(tokens[-1]) if prefix else window[-1] == tokens[-1]
            ):
                count += 1
        return count

    def idf(self, held: int) -> float:
        value = math.log((len(self.rows) - held + 0.5) / (held + 0.5))
        return value if value > 0 else 1e-6

    def score(self, n: int, hits: Sequence[dict[int, tuple]], idfs: Sequence[float], weights: Sequence[float]) -> float:
        """FTS5's bm25 of row ``n`` for phrases with ``hits`` and ``idfs``, columns weighed by ``weights``."""
        avgdl = self.avgdl
        norm = K1 * (1 - B + B * self.sizes[n] / avgdl) if avgdl else K1 * (1 - B)
        score = 0.0
        for found, idf in zip(hits, idfs, strict=True):
            counts = found.get(n)
            if counts is None:
                continue
            f = sum(w * c for w, c in zip(weights, counts, strict=False))
            score += idf * (f * (K1 + 1)) / (f + norm)
        return -score

    def matching_rows(self, phrases: list[Phrase], tree: tuple, among: set[int]) -> set[int]:
        """The rows of ``among`` that the query of ``phrases`` and ``tree`` (``parse``) matches."""
        if not self.fast:
            return matching(tree, [self.hits(p) for p in phrases]) & among
        np = _np
        assert np is not None
        docs = _tree_docs(tree, [_phrase_arrays(self, p)[0] for p in phrases])
        return set(np.intersect1d(docs, np.fromiter(among, dtype=np.int64)).tolist())

    def search(self, query: str, weights: Sequence[float], keep=None) -> list[tuple[int, float]]:
        """(row, bm25) of the rows matching ``query`` and kept by ``keep`` (a callable of a row, or a mask over the
        rows), by row; [] for a query FTS5 cannot read."""
        try:
            phrases, tree = parse(query)
        except QueryError:
            return []
        if self.fast:
            return _exhaustive(self, phrases, tree, weights, keep, None)
        hits = [self.hits(p) for p in phrases]
        idfs = [self.idf(len(h)) for h in hits]
        return [(n, self.score(n, hits, idfs, weights)) for n in sorted(matching(tree, hits)) if _kept(keep, n)]


def wand_top_k(table: Table, query: str, weights: Sequence[float], k: int, keep=None) -> list[tuple[int, float]]:
    """(row, bm25) of the ``k`` best rows matching ``query`` and kept by ``keep``, by row, every row tied with the
    ``k``-th best included: exactly the rows of ``table.search`` a sort by score and any tie-break then cut to ``k``
    can keep, each with the same score.

    With numpy a query of one phrase, or of phrases ORed, runs Block-Max WAND: the rows fall into blocks of BLOCK,
    each phrase's best possible score in each block bounds the block's, and blocks are scored best bound first until
    no block left can reach the ``k``-th score so far. Any other query, and every query without numpy, scores all
    the rows it matches."""
    try:
        phrases, tree = parse(query)
    except QueryError:
        return []
    if not table.fast:
        return _cut(table.search(query, weights, keep), k)
    if k <= 0 or not phrases:
        return []
    if tree[0] == "p" or (tree[0] == "or" and all(child[0] == "p" for child in tree[1])):
        return _block_max(table, phrases, weights, k, keep)
    return _exhaustive(table, phrases, tree, weights, keep, k)


# -- the numpy path --------------------------------------------------------------------------------------------------

try:
    import numpy as _np
except ImportError:  # the search reads the postings in pure Python
    _np = None

BLOCK = 512  # rows per block of Block-Max WAND
SLACK = 1e-9  # how much a block's bound is raised before it is compared: its float64 sum rounds too


def _phrase_arrays(table: Table, phrase: Phrase):
    """(rows ascending, hits per column) of the rows holding ``phrase``, as numpy arrays."""
    np = _np
    assert np is not None
    tokens = table.tokenize(phrase.text)
    empty = np.zeros(0, dtype=np.int64), np.zeros((0, table.columns), dtype=np.uint32)
    if not tokens:
        return empty
    if len(tokens) > 1 and table.tokenize is trigrams and _APART not in phrase.text.lower():
        return _substring_rows(table, phrase.text.lower().encode())
    if len(tokens) > 1:
        found = table._sequence(tokens, phrase.prefix, phrase.text)
        if not found:
            return empty
        return np.fromiter(found, dtype=np.int64), np.array(list(found.values()), dtype=np.uint32)
    if phrase.prefix:
        first, last = table._prefixed(tokens[0])
        start, end = (table.offsets[first], table.offsets[last]) if last > first else (0, 0)
    else:
        start, end = table._span(tokens[0])
        last = first = 0
    docs, counts = table.docs[start:end].astype(np.int64), table.counts[start:end]
    if phrase.prefix and last - first > 1 and len(docs):
        order = np.argsort(docs, kind="stable")
        docs, counts = docs[order], counts[order]
        heads = np.flatnonzero(np.concatenate(([True], docs[1:] != docs[:-1])))
        docs, counts = docs[heads], np.add.reduceat(counts, heads, axis=0)
    return docs, counts


def _unique(rows):
    """``rows`` sorted, each once: by a sort, which is faster than numpy's hashing ``unique`` on these."""
    np = _np
    assert np is not None
    rows = np.sort(rows)
    return rows[np.concatenate(([True], rows[1:] != rows[:-1]))] if len(rows) else rows


def _substring_rows(table: Table, sought: bytes):
    """(rows ascending, hits per column) of the rows whose text, case folded, holds ``sought`` (``_substrings``)."""
    np = _np
    assert np is not None
    if table.joined is None:
        table.joined = [table._joined(c) for c in range(table.columns)]
    pattern = np.frombuffer(sought, dtype=np.uint8)
    per_column = []
    for whole, starts in table.joined:
        if len(whole) < len(pattern):
            per_column.append(np.zeros(0, dtype=np.int64))
            continue
        at = np.flatnonzero(whole[: len(whole) - len(pattern) + 1] == pattern[0])
        for j in range(1, len(pattern)):
            at = at[whole[at + j] == pattern[j]]
        per_column.append(np.searchsorted(starts, at, side="right") - 1)
    rows = _unique(np.concatenate(per_column)) if per_column else np.zeros(0, dtype=np.int64)
    counts = np.zeros((len(rows), table.columns), dtype=np.uint32)
    for c, held in enumerate(per_column):
        counts[:, c] = np.bincount(np.searchsorted(rows, held), minlength=len(rows))
    return rows, counts


def _candidates(table: Table, tokens: list[str], last: list[str]) -> list[int]:
    np = _np
    assert np is not None
    found = None
    for t in tokens:
        start, end = table._span(t)
        rows = table.docs[start:end]
        found = rows if found is None else np.intersect1d(found, rows, assume_unique=True)
    spans = [table._span(t) for t in last]
    ends = _unique(np.concatenate([table.docs[a:b] for a, b in spans])) if spans else np.zeros(0, dtype=np.int32)
    return np.intersect1d(found, ends, assume_unique=True).tolist()


def _parts(table: Table, phrase: Phrase, docs, counts):
    """(blocks, most hits per column, least norm) of the blocks holding a phrase's ``docs``: what its bound in each is
    made of, whatever the weights; kept per token of a phrase of one."""
    np = _np
    assert np is not None
    tokens = table.tokenize(phrase.text)
    key = table._span(tokens[0]) if len(tokens) == 1 and not phrase.prefix else None
    held = table.bounds.get(key) if key is not None else None
    if held is not None:
        return held
    blocks = docs // BLOCK
    heads = np.flatnonzero(np.concatenate(([True], blocks[1:] != blocks[:-1])))
    held = blocks[heads], np.maximum.reduceat(counts, heads, axis=0), np.minimum.reduceat(table.norm[docs], heads)
    if key is not None:
        table.bounds[key] = held
    return held


def _weighed(counts, weights: Sequence[float]):
    """f of each row: its hits per column times the column's weight, summed in ``score``'s order."""
    np = _np
    assert np is not None
    f = np.zeros(len(counts))
    for c, w in zip(range(counts.shape[1]), weights, strict=False):
        f = f + w * counts[:, c].astype(np.float64)
    return f


def _tree_docs(tree: tuple, docs: list):
    np = _np
    assert np is not None
    if tree[0] == "p":
        return docs[tree[1]]
    parts = [_tree_docs(child, docs) for child in tree[1]]
    out = parts[0]
    for part in parts[1:]:
        out = np.intersect1d(out, part, assume_unique=True) if tree[0] == "and" else np.union1d(out, part)
    return out


def _scores(table: Table, rows, found: list, idfs: list[float], weights: Sequence[float]):
    """The bm25 of each row of ``rows`` (ascending) for phrases ``found`` (``_phrase_arrays``), as ``score`` sums it."""
    np = _np
    assert np is not None
    total = np.zeros(len(rows))
    for (docs, counts), idf in zip(found, idfs, strict=True):
        if not len(docs) or not len(rows):
            continue
        at = np.minimum(np.searchsorted(docs, rows), len(docs) - 1)
        there = np.flatnonzero(docs[at] == rows)
        f = _weighed(counts[at[there]], weights)
        total[there] = total[there] + idf * (f * (K1 + 1)) / (f + table.norm[rows[there]])
    return -total


def _keep_rows(rows, keep):
    """The rows of ``rows`` ``keep`` keeps: a mask over the table's rows, or a callable of one."""
    np = _np
    assert np is not None
    if keep is None or not len(rows):
        return rows
    if callable(keep):
        return rows[np.fromiter((bool(keep(n)) for n in rows.tolist()), dtype=bool, count=len(rows))]
    return rows[np.asarray(keep, dtype=bool)[rows]]


def _top(rows, scores, k: int | None) -> list[tuple[int, float]]:
    np = _np
    assert np is not None
    if k is not None and len(rows) > k:
        kth = np.partition(scores, k - 1)[k - 1]
        best = scores <= kth
        rows, scores = rows[best], scores[best]
    return list(zip(rows.tolist(), scores.tolist(), strict=True))


def _exhaustive(table: Table, phrases, tree, weights, keep, k: int | None) -> list[tuple[int, float]]:
    found = [_phrase_arrays(table, p) for p in phrases]
    idfs = [table.idf(len(docs)) for docs, _ in found]
    rows = _keep_rows(_tree_docs(tree, [docs for docs, _ in found]), keep)
    return _top(rows, _scores(table, rows, found, idfs, weights), k)


def _block_max(table: Table, phrases, weights, k: int, keep) -> list[tuple[int, float]]:
    np = _np
    assert np is not None
    found = [_phrase_arrays(table, p) for p in phrases]
    idfs = [table.idf(len(docs)) for docs, _ in found]
    bounds = np.zeros((len(table.rows) + BLOCK - 1) // BLOCK)
    touched = np.zeros(len(bounds), dtype=bool)
    for phrase, (docs, counts), idf in zip(phrases, found, idfs, strict=True):
        if len(docs):
            blocks, most, least = _parts(table, phrase, docs, counts)
            f = _weighed(most, weights)
            # no row of a block scores above this: f at most the block's most hits, the norm at least its least
            bounds[blocks] += idf * (f * (K1 + 1)) / (f + least)
            touched[blocks] = True
    order = np.flatnonzero(touched)
    order = order[np.argsort(-bounds[order], kind="stable")]
    rows_of = [docs // BLOCK for docs, _ in found]
    kept_rows, kept_scores = [np.zeros(0, dtype=np.int64)], [np.zeros(0)]
    held, kth, at, step = 0, None, 0, 1
    while at < len(order):
        if kth is not None and bounds[order[at]] * (1 + SLACK) < kth:
            break  # no block left can reach the k-th best score: they come best bound first
        chosen = np.zeros(len(bounds), dtype=bool)
        chosen[order[at : at + step]] = True
        at, step = at + step, step * 2
        rows = _unique(np.concatenate([docs[chosen[b]] for (docs, _), b in zip(found, rows_of, strict=True)]))
        rows = _keep_rows(rows, keep)
        kept_rows.append(rows)
        kept_scores.append(-_scores(table, rows, found, idfs, weights))
        held += len(rows)
        if held >= k:
            every = np.concatenate(kept_scores)
            kth = float(np.partition(every, len(every) - k)[len(every) - k])
    rows, scores = np.concatenate(kept_rows), -np.concatenate(kept_scores)
    order = np.argsort(rows, kind="stable")
    return _top(rows[order], scores[order], k)


def _updated(table: Table, rows: Sequence[Sequence[str]], old: Sequence[int]) -> Table | None:
    """``Table.updated`` with numpy; None when the rows kept are not in their order in ``table``."""
    np = _np
    assert np is not None
    out = Table.__new__(Table)
    out.rows = tuple(r if isinstance(r, tuple) else tuple(r) for r in rows)
    out.tokenize, out.columns = table.tokenize, table.columns
    before = np.asarray(old, dtype=np.int64).reshape(-1)
    kept = before >= 0
    if not table.columns or not np.all(np.diff(before[kept]) > 0):
        return None
    fresh = np.flatnonzero(~kept)
    postings, sizes = _postings([out.rows[j] for j in fresh], fresh.tolist(), out.tokenize, out.columns)
    remap = np.full(len(table.rows), -1, dtype=np.int64)
    remap[before[kept]] = np.flatnonzero(kept)
    added = sorted(t for t in postings if t not in table.token_ids)
    shift, merged = _vocabulary(table.vocabulary, added)
    _merge_records(out, table, remap, _new_records(table, postings, shift, merged), (shift, merged))
    sized = np.zeros(len(out.rows), dtype=np.uint32)
    if table.sizes:
        sized[kept] = np.frombuffer(table.sizes, dtype=np.uint32)[before[kept]]
    if len(sizes):
        sized[fresh] = np.frombuffer(sizes, dtype=np.uint32)
    out.sizes = array("I")
    out.sizes.frombytes(sized.tobytes())
    out.total = int(sized.sum(dtype=np.uint64))
    out.avgdl = out.total / len(out.rows) if out.rows else 0.0
    out.flat, out.fast = None, True
    out._normed()
    out.joined = _joined_updated(out, table, before, fresh) if out.tokenize is trigrams else None
    out.bounds = {}
    return out


def _vocabulary(vocabulary: list[str], added: list[str]):
    """(each old token's number among the tokens of both, the tokens of both, sorted)."""
    np = _np
    assert np is not None
    at = [bisect_left(vocabulary, t) for t in added]
    shift = np.arange(len(vocabulary), dtype=np.int64) + np.searchsorted(
        np.asarray(at, dtype=np.int64), np.arange(len(vocabulary)), side="right"
    )
    if not added:
        return shift, vocabulary
    merged: list[str] = []
    last = 0
    for i, t in zip(at, added, strict=True):
        merged.extend(vocabulary[last:i])
        merged.append(t)
        last = i
    merged.extend(vocabulary[last:])
    return shift, merged


def _new_records(table: Table, postings: dict[str, array], shift, merged: list[str]) -> tuple:
    """The records of the new rows, by token then row: (token number among ``merged``, its number in ``table`` or
    -1, where ``table``'s records of the tokens after it start, [row, c0, ...])."""
    np = _np
    assert np is not None
    width = table.columns + 1
    if not postings:
        return _ints(0), _ints(0), _ints(0), np.zeros((0, width), dtype=np.uint32)
    offsets = np.frombuffer(table.offsets, dtype=np.int64)
    olds, number, start = [], [], []
    for t in postings:
        i = table.token_ids.get(t)
        olds.append(-1 if i is None else i)
        number.append(int(shift[i]) if i is not None else bisect_left(merged, t))
        start.append(0 if i is not None else int(offsets[bisect_left(table.vocabulary, t)]))
    per = [len(p) // width for p in postings.values()]
    records = np.concatenate([np.frombuffer(p, dtype=np.uint32) for p in postings.values()]).reshape(-1, width)
    tokens, olds, start = (np.repeat(np.asarray(x, dtype=np.int64), per) for x in (number, olds, start))
    order = np.lexsort((records[:, 0], tokens))
    return tokens[order], olds[order], start[order], records[order]


def _ints(n: int):
    assert _np is not None
    return _np.zeros(n, dtype=_np.int64)


def _merge_records(out: Table, table: Table, remap, new: tuple, vocabulary: tuple) -> None:
    """Give ``out`` the CSR of ``table``'s records of the rows ``remap`` keeps, renumbered by it, with the records
    ``new`` (``_new_records``) put in their places, over the tokens of ``vocabulary`` (shift, merged) some record
    holds."""
    np = _np
    assert np is not None
    shift, merged = vocabulary
    tokens, olds, start, records = new
    offsets = np.frombuffer(table.offsets, dtype=np.int64)
    rows = len(out.rows) + 1
    docs = remap[table.docs] if len(table.docs) else _ints(0)
    held = docs >= 0
    # where a new record goes among the old ones: by (token, row), each old row placed by its number after the update,
    # one dropped between those of the rows kept around it
    rank = np.maximum.accumulate(remap) if len(remap) else np.zeros(0)
    rank = rank + np.where(remap >= 0, 0.0, 0.5)
    spans = np.diff(offsets)
    key = np.repeat(np.arange(len(spans), dtype=np.int64), spans) * rows + rank[table.docs]
    at = np.where(olds >= 0, np.searchsorted(key, olds * rows + records[:, 0]), start)
    before = np.zeros(len(held) + 1, dtype=np.int64)
    np.cumsum(held, dtype=np.int64, out=before[1:])
    placed = before[at] + np.arange(len(at), dtype=np.int64)
    size = int(before[-1]) + len(at)
    out.docs = np.empty(size, dtype=np.int32)
    _copy_kept(out.docs, docs, held, placed)
    out.docs[placed] = records[:, 0]
    out.counts = np.empty((size, table.columns), dtype=np.uint32)
    _copy_kept(out.counts, table.counts, held, placed)
    out.counts[placed] = records[:, 1:]
    per = np.zeros(len(merged), dtype=np.int64)
    per[shift] = before[offsets[1:]] - before[offsets[:-1]]
    if len(tokens):
        per += np.bincount(tokens, minlength=len(merged))
    used = per > 0
    out.vocabulary = [t for t, h in zip(merged, used.tolist(), strict=True) if h]
    out.token_ids = {t: i for i, t in enumerate(out.vocabulary)}
    ends = np.zeros(len(out.vocabulary) + 1, dtype=np.int64)
    np.cumsum(per[used], out=ends[1:])
    out.offsets = array("q")
    out.offsets.frombytes(ends.tobytes())


RUNS = 4096  # new records up to which ``_copy_kept`` copies the old ones run by run, past it through a mask


def _copy_kept(out, counts, held, placed) -> None:
    """``out[free] = counts[held]``, ``free`` the rows of ``out`` not in ``placed`` (ascending): for a delta's few new
    records, as slices of the runs between them, a fraction of the time of a boolean mask over a million rows."""
    np = _np
    assert np is not None
    kept = counts if bool(held.all()) else np.compress(held, counts, axis=0)
    if len(placed) > RUNS:
        free = np.ones(len(out), dtype=bool)
        free[placed] = False
        out[free] = kept
        return
    taken = last = 0
    for at in placed.tolist():
        out[last:at] = kept[taken : taken + at - last]
        taken += at - last
        last = at + 1
    out[last:] = kept[taken:]


def _joined_updated(out: Table, table: Table, before, fresh) -> list[tuple]:
    """``out``'s texts joined per column (``Table._joined``): the bytes of the rows kept copied from ``table``'s."""
    np = _np
    assert np is not None
    if table.joined is None or not len(table.rows):
        return [out._joined(c) for c in range(out.columns)]
    kept = np.flatnonzero(before >= 0)
    joined = []
    for c, (whole, starts) in enumerate(table.joined):
        sizes = np.diff(np.append(starts, len(whole) + 1)) - 1
        encoded = [(out.rows[j][c] or "").lower().encode() for j in fresh.tolist()]
        length = np.zeros(len(out.rows), dtype=np.int64)
        length[kept] = sizes[before[kept]]
        length[fresh] = [len(e) for e in encoded]
        at = np.zeros(len(out.rows), dtype=np.int64)
        np.cumsum(length[:-1] + 1, out=at[1:])
        text = np.full(int(length.sum()) + max(len(out.rows) - 1, 0), ord(_APART), dtype=np.uint8)
        copied = length[kept]
        within = np.arange(int(copied.sum()), dtype=np.int64) - np.repeat(np.cumsum(copied) - copied, copied)
        text[np.repeat(at[kept], copied) + within] = whole[np.repeat(starts[before[kept]], copied) + within]
        for j, e in zip(fresh.tolist(), encoded, strict=True):
            text[at[j] : at[j] + len(e)] = np.frombuffer(e, dtype=np.uint8)
        joined.append((text, at))
    return joined
